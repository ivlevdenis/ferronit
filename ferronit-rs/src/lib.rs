/// Ferronit native core — Router, Request parser, JSON, full ASGI app.
use matchit::Router as MatchitRouter;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};
use std::collections::HashMap;

// ── Router ────────────────────────────────────────────────────────────

/// Роутер, разбитый по HTTP-методу: у каждого метода свой matchit-роутер по
/// путям (без префикса метода). Метод резолвится в готовый роутер за O(1),
/// поэтому в горячем пути `lookup` не склеивает ``"METHOD /path"`` в новую
/// строку на каждый запрос — идёт чистый ``at(path)`` без аллокации.
#[pyclass]
pub struct Router {
    inner: HashMap<String, MatchitRouter<PyObject>>,
}

#[pymethods]
impl Router {
    #[new]
    fn new() -> Self {
        Router {
            inner: HashMap::new(),
        }
    }

    fn add(&mut self, method: String, pattern: String, handler: PyObject) -> PyResult<()> {
        let router = self
            .inner
            .entry(method)
            .or_insert_with(|| MatchitRouter::new());
        // matchit нумерует параметры буквами a..z и ПАНИКУЕТ на шаблоне с >25
        // параметрами («Too many route parameters.»), а не возвращает ошибку.
        // Паника через FFI стала бы PanicException (BaseException) и обошла бы
        // документированный отказ ValueError — превращаем её в обычную ошибку.
        // Предпроверка нужна, чтобы не писать в stderr паническую простыню:
        // catch_unwind оставлен вторым рубежом на прочие паники matchit.
        if pattern.matches('{').count() > 25 {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "Invalid route: too many path parameters (at most 25)",
            ));
        }
        let inserted = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            router.insert(pattern, handler)
        }));
        match inserted {
            Ok(Ok(_)) => Ok(()),
            Ok(Err(e)) => Err(pyo3::exceptions::PyValueError::new_err(format!(
                "Invalid route: {}",
                e
            ))),
            Err(_) => Err(pyo3::exceptions::PyValueError::new_err(
                "Invalid route: too many path parameters (at most 25)",
            )),
        }
    }

    /// Fast path lookup. Returns (handler, params) where params is None
    /// when the route has no path parameters (the common case — avoids
    /// allocating a HashMap on every request).
    fn lookup(
        &self,
        py: Python<'_>,
        method: &str,
        path: &str,
    ) -> PyResult<Option<(PyObject, Option<HashMap<String, String>>)>> {
        let router = match self.inner.get(method) {
            Some(router) => router,
            None => return Ok(None),
        };
        match router.at(path) {
            Ok(matched) => {
                let handler = matched.value.clone_ref(py);
                if matched.params.is_empty() {
                    return Ok(Some((handler, None)));
                }
                let mut params = HashMap::with_capacity(matched.params.len());
                for (k, v) in matched.params.iter() {
                    params.insert(k.to_string(), v.to_string());
                }
                Ok(Some((handler, Some(params))))
            }
            Err(_) => Ok(None),
        }
    }
}

// ── Request ──────────────────────────────────────────────────────────

#[pyclass]
pub struct Request {
    #[pyo3(get)]
    method: String,
    #[pyo3(get)]
    path: String,
    #[pyo3(get)]
    query_string: String,
    raw_headers: Vec<(Vec<u8>, Vec<u8>)>,
    body: Option<Vec<u8>>,
}

#[pymethods]
impl Request {
    #[new]
    fn py_new(method: String, path: String, query_string: String) -> Self {
        Request {
            method,
            path,
            query_string,
            raw_headers: Vec::new(),
            body: None,
        }
    }

    /// Lazy header parsing: headers are only materialised as a PyDict
    /// when `.headers` is actually accessed.
    #[getter]
    fn headers(&self, py: Python<'_>) -> PyResult<PyObject> {
        let dict = PyDict::new(py);
        for (k, v) in &self.raw_headers {
            // HTTP headers are ASCII — ascii lowercase is one alloc, no UTF-8 pass
            let key = String::from_utf8_lossy(k).to_ascii_lowercase();
            let val = String::from_utf8_lossy(v);
            let _ = dict.set_item(key, &*val);
        }
        Ok(dict.into())
    }

    #[getter]
    fn query(&self, py: Python<'_>) -> PyResult<PyObject> {
        let dict = PyDict::new(py);
        if self.query_string.is_empty() {
            return Ok(dict.into());
        }
        for pair in self.query_string.split('&') {
            if let Some((k, v)) = pair.split_once('=') {
                let key = url_decode(k);
                let val = url_decode(v);
                match dict.get_item(&key) {
                    Ok(Some(list_obj)) => {
                        if let Ok(list) = list_obj.downcast::<PyList>() {
                            list.append(val)?;
                        }
                    }
                    _ => {
                        let list = PyList::new(py, vec![val])?;
                        dict.set_item(key, list)?;
                    }
                }
            }
        }
        Ok(dict.into())
    }

    /// Store raw headers without parsing (parsing happens lazily).
    fn set_headers(&mut self, raw_headers: Vec<(Vec<u8>, Vec<u8>)>) {
        self.raw_headers = raw_headers;
    }

    /// O(1)-ish lookup of a single header by name — no full parse.
    fn get_header(&self, name: &str) -> Option<String> {
        for (k, v) in &self.raw_headers {
            if k.eq_ignore_ascii_case(name.as_bytes()) {
                return Some(String::from_utf8_lossy(v).into_owned());
            }
        }
        None
    }

    fn set_body(&mut self, body: Vec<u8>) {
        self.body = Some(body);
    }

    /// Parse a ``multipart/form-data`` body.
    ///
    /// Returns ``(fields, files)``: fields — ``dict[str, list[str]]``, files —
    /// ``dict[str, list[(filename, content_type, bytes)]]``. The body must be
    /// stored first via :meth:`set_body`.
    fn parse_multipart(&self, py: Python<'_>, boundary: String) -> PyResult<(PyObject, PyObject)> {
        let body = self.body.as_deref().unwrap_or(b"");
        let delim = format!("--{boundary}");
        let fields = PyDict::new(py);
        let files = PyDict::new(py);

        for chunk in split_bytes(body, delim.as_bytes()) {
            if chunk.is_empty() {
                continue;
            }
            if chunk.starts_with(b"--") {
                break; // финальный --boundary--
            }
            let chunk = chunk.strip_prefix(b"\r\n").unwrap_or(chunk);
            let Some((head, mut content)) = split_once_double_crlf(chunk) else {
                continue;
            };
            if content.ends_with(b"\r\n") {
                content = &content[..content.len() - 2];
            }
            let (name, filename, content_type) = parse_part_headers(head);
            let Some(name) = name else { continue };
            if let Some(filename) = filename {
                let item = (filename, content_type, PyBytes::new(py, content));
                match files.get_item(&name)? {
                    Some(existing) => existing.downcast::<PyList>()?.append(item)?,
                    None => {
                        files.set_item(&name, PyList::new(py, [item])?)?;
                    }
                }
            } else {
                let text = String::from_utf8_lossy(content).into_owned();
                match fields.get_item(&name)? {
                    Some(existing) => existing.downcast::<PyList>()?.append(text)?,
                    None => {
                        fields.set_item(&name, PyList::new(py, [text])?)?;
                    }
                }
            }
        }
        Ok((fields.into(), files.into()))
    }
}

fn url_decode(s: &str) -> String {
    let mut bytes = Vec::with_capacity(s.len());
    // Python отдаёт query_string как latin-1 (байты сохраняются 1:1), поэтому
    // каждый char <= 0xFF — это ровно один исходный байт, а не UTF-8-кодпоинт.
    let mut chars = s.chars();
    while let Some(c) = chars.next() {
        if c == '+' {
            bytes.push(b' ');
        } else if c == '%' {
            let h = chars.by_ref().take(2).collect::<String>();
            if let Ok(byte) = u8::from_str_radix(&h, 16) {
                bytes.push(byte);
            } else {
                bytes.push(b'%');
                bytes.extend_from_slice(h.as_bytes());
            }
        } else if (c as u32) <= 0xFF {
            bytes.push(c as u8);
        } else {
            let mut buf = [0u8; 4];
            bytes.extend_from_slice(c.encode_utf8(&mut buf).as_bytes());
        }
    }
    match String::from_utf8(bytes) {
        Ok(text) => text,
        // не UTF-8 — значит пришла настоящая latin-1-строка: разворачиваем побайтово
        Err(e) => e.into_bytes().iter().map(|&b| b as char).collect(),
    }
}

fn split_once_double_crlf(data: &[u8]) -> Option<(&[u8], &[u8])> {
    let needle = b"\r\n\r\n";
    let pos = data.windows(needle.len()).position(|w| w == needle)?;
    Some((&data[..pos], &data[pos + needle.len()..]))
}

fn find_subslice(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    if needle.is_empty() {
        return Some(0);
    }
    haystack.windows(needle.len()).position(|w| w == needle)
}

fn split_bytes<'a>(data: &'a [u8], sep: &[u8]) -> Vec<&'a [u8]> {
    let mut out = Vec::new();
    let mut rest = data;
    while !rest.is_empty() {
        match find_subslice(rest, sep) {
            Some(pos) => {
                let (chunk, tail) = rest.split_at(pos);
                out.push(chunk);
                rest = &tail[sep.len()..];
            }
            None => {
                out.push(rest);
                break;
            }
        }
    }
    out
}

fn parse_part_headers(head: &[u8]) -> (Option<String>, Option<String>, String) {
    let mut name = None;
    let mut filename = None;
    let mut content_type = String::new();
    for line in head.split(|&b| b == b'\n') {
        let line = line.strip_suffix(b"\r").unwrap_or(line);
        let Some(colon) = line.iter().position(|&b| b == b':') else {
            continue;
        };
        let key = String::from_utf8_lossy(&line[..colon]).trim().to_ascii_lowercase();
        let value = String::from_utf8_lossy(&line[colon + 1..]).trim().to_string();
        if key == "content-disposition" {
            let (n, f) = parse_disposition(&value);
            name = n;
            filename = f;
        } else if key == "content-type" {
            content_type = value;
        }
    }
    (name, filename, content_type)
}

fn parse_disposition(value: &str) -> (Option<String>, Option<String>) {
    let mut name = None;
    let mut filename = None;
    for piece in value.split(';') {
        let piece = piece.trim();
        let Some(eq) = piece.find('=') else { continue };
        let key = piece[..eq].trim().to_ascii_lowercase();
        let mut val = piece[eq + 1..].trim();
        if val.len() >= 2 && val.starts_with('"') && val.ends_with('"') {
            val = &val[1..val.len() - 1];
        }
        if key == "name" {
            name = Some(val.to_string());
        } else if key == "filename" {
            filename = Some(val.to_string());
        } else if key == "filename*" {
            // RFC 5987: filename*=UTF-8''<percent-encoded> — предпочитаем его
            filename = Some(match val.find("''") {
                Some(pos) => percent_decode(&val[pos + 2..]),
                None => percent_decode(val),
            });
        }
    }
    (name, filename)
}

fn percent_decode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            if let (Some(h), Some(l)) = (hex_val(bytes[i + 1]), hex_val(bytes[i + 2])) {
                out.push(h * 16 + l);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn hex_val(b: u8) -> Option<u8> {
    match b {
        b'0'..=b'9' => Some(b - b'0'),
        b'a'..=b'f' => Some(b - b'a' + 10),
        b'A'..=b'F' => Some(b - b'A' + 10),
        _ => None,
    }
}

// ── Response Builder ──────────────────────────────────────────────────

#[pyclass]
pub struct Response {
    #[pyo3(get)]
    status: u16,
    #[pyo3(get)]
    body: Py<PyBytes>,
    #[pyo3(get)]
    content_type: String,
}

#[pymethods]
impl Response {
    #[new]
    fn py_new(body: Vec<u8>, status: u16, content_type: String, py: Python<'_>) -> Self {
        Response {
            body: PyBytes::new(py, &body).into(),
            status,
            content_type,
        }
    }

    #[staticmethod]
    fn text(text: String, status: Option<u16>, py: Python<'_>) -> Self {
        let body = text.into_bytes();
        Response {
            body: PyBytes::new(py, &body).into(),
            status: status.unwrap_or(200),
            content_type: "text/plain".to_string(),
        }
    }
}

// ── ASGI App ──────────────────────────────────────────────────────────

/// Full Rust-native ASGI application.
/// Owns the router, receives scope/receive/send from Python, dispatches.
#[pyclass]
pub struct FerronitApp {
    router: Router,
    cors_origins: Vec<String>,
    cors_methods: Option<String>,
    cors_headers: Option<String>,
    cors_max_age: Option<String>,
}

#[pymethods]
impl FerronitApp {
    #[new]
    fn new() -> Self {
        FerronitApp {
            router: Router::new(),
            cors_origins: Vec::new(),
            cors_methods: None,
            cors_headers: None,
            cors_max_age: None,
        }
    }

    fn set_cors(
        &mut self,
        origins: Option<String>,
        methods: Option<String>,
        headers: Option<String>,
        max_age: Option<String>,
    ) {
        self.cors_origins = origins
            .map(|o| o.split(',').map(|s| s.trim().to_string()).collect())
            .unwrap_or_else(|| vec!["*".to_string()]);
        self.cors_methods = methods.or(Some("GET,POST,PUT,DELETE,OPTIONS,PATCH".to_string()));
        self.cors_headers = headers.or(Some("*".to_string()));
        self.cors_max_age = max_age.or(Some("600".to_string()));
    }

    fn cors_allows(&self, origin: &str) -> bool {
        self.cors_origins.iter().any(|o| o == "*" || o == origin)
    }

    fn cors_is_wildcard(&self) -> bool {
        self.cors_origins.iter().any(|o| o == "*")
    }

    fn add_route(&mut self, method: String, path: String, handler: PyObject) -> PyResult<()> {
        self.router.add(method, path, handler)
    }

    /// Fast routing lookup — returns (handler, params) or None.
    fn resolve(
        &self,
        py: Python<'_>,
        method: &str,
        path: &str,
    ) -> PyResult<Option<(PyObject, Option<HashMap<String, String>>)>> {
        self.router.lookup(py, method, path)
    }

    /// Check if CORS is configured.
    fn has_cors(&self) -> bool {
        !self.cors_origins.is_empty()
    }

    /// CORS preflight headers — only when the request Origin is allowed.
    fn cors_preflight_headers(
        &self,
        py: Python<'_>,
        origin: &str,
    ) -> PyResult<Option<PyObject>> {
        if !self.cors_allows(origin) {
            return Ok(None);
        }
        let dict = PyDict::new(py);
        let allow_origin = if self.cors_is_wildcard() {
            "*".to_string()
        } else {
            origin.to_string()
        };
        dict.set_item("Access-Control-Allow-Origin", allow_origin)?;
        if let Some(ref m) = self.cors_methods {
            dict.set_item("Access-Control-Allow-Methods", m)?;
        }
        if let Some(ref h) = self.cors_headers {
            dict.set_item("Access-Control-Allow-Headers", h)?;
        }
        if let Some(ref a) = self.cors_max_age {
            dict.set_item("Access-Control-Max-Age", a)?;
        }
        Ok(Some(dict.into()))
    }

    /// CORS origin header for normal responses — only when the Origin is allowed.
    fn cors_origin(&self, origin: &str) -> Option<String> {
        if self.cors_allows(origin) {
            Some(if self.cors_is_wildcard() {
                "*".to_string()
            } else {
                origin.to_string()
            })
        } else {
            None
        }
    }

    /// GZip compress a byte buffer. Returns compressed bytes.
    fn gzip_compress(&self, _py: Python<'_>, data: Vec<u8>) -> PyResult<Vec<u8>> {
        use std::io::Write;
        let mut encoder = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::fast());
        encoder.write_all(&data).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("GZip error: {}", e))
        })?;
        encoder.finish().map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("GZip finish: {}", e))
        })
    }
}

// ── Module ────────────────────────────────────────────────────────────

#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Router>()?;
    m.add_class::<Request>()?;
    m.add_class::<Response>()?;
    m.add_class::<FerronitApp>()?;
    Ok(())
}
