/// Ferronit native core — Router, Request parser, JSON, full ASGI app.
use matchit::Router as MatchitRouter;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList, PyString, PyTuple};
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
    /// Raw transport pairs, kept as the Python object: the ASGI scope list is
    /// just incref'ed, so no per-pair conversion happens unless `.headers` is
    /// read. `Py<T>` drops safely without the GIL (`register_decref`).
    raw_headers: Option<Py<PyAny>>,
    body: Option<Vec<u8>>,
}

/// Decode a header name/value from either transport form.
///
/// ASGI gives ``bytes``, the RSGI adapter gives ``str``; both are decoded here
/// so the Python side never has to build an intermediate bytes list.
fn header_text(obj: &Bound<'_, PyAny>) -> PyResult<String> {
    if let Ok(text) = obj.downcast::<PyString>() {
        return Ok(text.to_str()?.to_owned());
    }
    if let Ok(raw) = obj.downcast::<PyBytes>() {
        return Ok(String::from_utf8_lossy(raw.as_bytes()).into_owned());
    }
    Err(PyValueError::new_err(
        "header pairs must contain str or bytes",
    ))
}

/// Split one raw pair into its two objects (tuple fast path, sequence fallback).
fn pair_items<'py>(item: &Bound<'py, PyAny>) -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyAny>)> {
    match item.downcast::<PyTuple>() {
        Ok(tuple) if tuple.len() == 2 => Ok((tuple.get_item(0)?, tuple.get_item(1)?)),
        _ => item.extract::<(Bound<'_, PyAny>, Bound<'_, PyAny>)>(),
    }
}

/// Compare a raw name with `needle` without allocating: bytes are compared
/// case-insensitively in place, `str` through `eq_ignore_ascii_case`.
fn name_matches(obj: &Bound<'_, PyAny>, needle: &str) -> PyResult<bool> {
    if let Ok(raw) = obj.downcast::<PyBytes>() {
        let bytes = raw.as_bytes();
        return Ok(bytes.len() == needle.len() && bytes.eq_ignore_ascii_case(needle.as_bytes()));
    }
    if let Ok(text) = obj.downcast::<PyString>() {
        return Ok(text.to_str()?.eq_ignore_ascii_case(needle));
    }
    Ok(false)
}

/// Decode one raw pair into text (used by the dict builder).
fn pair_texts(item: Bound<'_, PyAny>) -> PyResult<(String, String)> {
    let (name, value) = pair_items(&item)?;
    Ok((header_text(&name)?, header_text(&value)?))
}

/// Build the RFC-combined header dict: `", "` for repeats, `"; "` for
/// ``cookie`` (RFC 9113 §8.2.3), first line only for ``set-cookie``
/// (RFC 6265 §4.1.1). Dict insertion order gives first-seen field order.
fn build_header_dict(py: Python<'_>, raw: &Bound<'_, PyAny>) -> PyResult<PyObject> {
    let dict = PyDict::new(py);
    for item in raw.try_iter()? {
        let (name, value) = pair_texts(item?)?;
        let mut key = name;
        key.make_ascii_lowercase();
        match dict.get_item(&key)? {
            Some(existing) => {
                if key == "set-cookie" {
                    continue;
                }
                let mut merged: String = existing.extract()?;
                merged.push_str(if key == "cookie" { "; " } else { ", " });
                merged.push_str(&value);
                dict.set_item(&key, merged)?;
            }
            None => dict.set_item(&key, value)?,
        }
    }
    Ok(dict.into())
}

#[pymethods]
impl Request {
    #[new]
    fn py_new(method: String, path: String, query_string: String) -> Self {
        Request {
            method,
            path,
            query_string,
            raw_headers: None,
            body: None,
        }
    }

    /// Headers as ``dict[str, str]`` with RFC 9110 §5.2 combination.
    ///
    /// Names are lowercased and keep first-seen order. Repeated fields are
    /// combined in wire order with ``", "`` — except ``cookie``, which uses the
    /// two-octet ``"; "`` delimiter mandated by RFC 9113 §8.2.3, and
    /// ``set-cookie``, which must never be combined (RFC 6265 §4.1.1) and keeps
    /// its first line. Values are accepted as ``bytes`` (ASGI) or ``str``
    /// (RSGI); undecodable bytes decay to U+FFFD instead of failing the request.
    ///
    /// Materialised lazily: nothing is parsed until this getter is read.
    #[getter]
    fn headers(&self, py: Python<'_>) -> PyResult<PyObject> {
        match &self.raw_headers {
            Some(raw) => build_header_dict(py, &raw.bind(py)),
            None => Ok(PyDict::new(py).into()),
        }
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

    /// First value of a repeated field, case-insensitive; no full parse.
    fn get_header(&self, py: Python<'_>, name: &str) -> PyResult<Option<String>> {
        let Some(raw) = &self.raw_headers else {
            return Ok(None);
        };
        for item in raw.bind(py).try_iter()? {
            let item = item?;
            let (key, value) = pair_items(&item)?;
            if name_matches(&key, name)? {
                return Ok(Some(header_text(&value)?));
            }
        }
        Ok(None)
    }

    /// Every value of a repeated field in wire order.
    fn get_all(&self, py: Python<'_>, name: &str) -> PyResult<Vec<String>> {
        let mut values = Vec::new();
        let Some(raw) = &self.raw_headers else {
            return Ok(values);
        };
        for item in raw.bind(py).try_iter()? {
            let item = item?;
            let (key, value) = pair_items(&item)?;
            if name_matches(&key, name)? {
                values.push(header_text(&value)?);
            }
        }
        Ok(values)
    }

    /// Cookies from every ``Cookie`` field (joined with ``"; "`` per RFC 9113
    /// §8.2.3), parsed as ``name=value`` pairs; quoted values are unquoted.
    #[getter]
    fn cookies(&self, py: Python<'_>) -> PyResult<PyObject> {
        let dict = PyDict::new(py);
        let Some(raw) = &self.raw_headers else {
            return Ok(dict.into());
        };
        let mut joined = String::new();
        for item in raw.bind(py).try_iter()? {
            let item = item?;
            let (key, value) = pair_items(&item)?;
            if name_matches(&key, "cookie")? {
                if !joined.is_empty() {
                    joined.push_str("; ");
                }
                joined.push_str(&header_text(&value)?);
            }
        }
        for part in joined.split(';') {
            let part = part.trim();
            let Some((name, value)) = part.split_once('=') else {
                continue;
            };
            let name = name.trim();
            if name.is_empty() {
                continue;
            }
            let value = value.trim();
            let value = value
                .strip_prefix('"')
                .and_then(|inner| inner.strip_suffix('"'))
                .unwrap_or(value);
            dict.set_item(name, value)?;
        }
        Ok(dict.into())
    }

    /// Store the transport's raw header pairs (an iterable of 2-item
    /// iterables of ``bytes`` or ``str``). Only increfs the object; parsing
    /// happens lazily on the first ``headers`` access.
    fn set_headers(&mut self, raw_headers: Py<PyAny>) {
        self.raw_headers = Some(raw_headers);
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

// ── Cookies ───────────────────────────────────────────────────────────

/// RFC 9110 §5.6.2 tchar; RFC 6265 §4.1.1 uses the same token for cookie names.
fn is_token_byte(b: u8) -> bool {
    matches!(
        b,
        b'!' | b'#'
            | b'$'
            | b'%'
            | b'&'
            | b'\''
            | b'*'
            | b'+'
            | b'-'
            | b'.'
            | b'^'
            | b'_'
            | b'`'
            | b'|'
            | b'~'
    ) || b.is_ascii_alphanumeric()
}

/// RFC 6265 §4.1.1 cookie-octet: excludes CTLs, SP, DQUOTE, comma, semicolon, backslash.
fn is_cookie_octet(b: u8) -> bool {
    b == 0x21
        || (0x23..=0x2B).contains(&b)
        || (0x2D..=0x3A).contains(&b)
        || (0x3C..=0x5B).contains(&b)
        || (0x5D..=0x7E).contains(&b)
}

/// RFC 6265 §4.1.1 path-value: `*( %x20-3A / %x3C-7E )` — no CTLs, no semicolon.
fn is_path_byte(b: u8) -> bool {
    (0x20..=0x3A).contains(&b) || (0x3C..=0x7E).contains(&b)
}

/// Conservative domain-value: letters, digits, hyphen and dot.
fn is_domain_byte(b: u8) -> bool {
    b.is_ascii_alphanumeric() || b == b'-' || b == b'.'
}

/// RFC 9110 field-vchar subset used for an already formatted HTTP-date.
fn is_attr_byte(b: u8) -> bool {
    (0x20..=0x7E).contains(&b) && b != b';'
}

fn invalid(part: &str, value: &str, reason: &str) -> PyErr {
    PyValueError::new_err(format!("invalid cookie {part} {value:?}: {reason}"))
}

/// Build a `Set-Cookie` field value with RFC 6265 §4.1.1 validation.
///
/// Every component is validated byte-wise; a value that cannot appear in a
/// cookie attribute is rejected instead of being escaped, so the returned
/// field value can never carry a CTL or a separator injection.
#[pyfunction]
#[pyo3(signature = (
    name,
    value = "",
    *,
    path = None,
    domain = None,
    max_age = None,
    expires = None,
    http_only = false,
    secure = false,
    same_site = None
))]
fn build_set_cookie(
    name: &str,
    value: &str,
    path: Option<&str>,
    domain: Option<&str>,
    max_age: Option<i64>,
    expires: Option<&str>,
    http_only: bool,
    secure: bool,
    same_site: Option<&str>,
) -> PyResult<String> {
    if name.is_empty() || !name.bytes().all(is_token_byte) {
        return Err(invalid("name", name, "expected a token"));
    }
    if !value.bytes().all(is_cookie_octet) {
        return Err(invalid(
            "value",
            value,
            "expected cookie-octet (no CTLs, SP, DQUOTE, comma, semicolon or backslash)",
        ));
    }
    if let Some(p) = path {
        if !p.bytes().all(is_path_byte) {
            return Err(invalid("path", p, "expected %x20-3A / %x3C-7E"));
        }
    }
    if let Some(d) = domain {
        if d.is_empty() || !d.bytes().all(is_domain_byte) {
            return Err(invalid("domain", d, "expected letters, digits, '-' or '.'"));
        }
    }
    if let Some(e) = expires {
        if e.is_empty() || !e.bytes().all(is_attr_byte) {
            return Err(invalid("expires", e, "expected an HTTP-date"));
        }
    }
    let same_site = match same_site {
        None => None,
        Some(raw) => Some(match raw.to_ascii_lowercase().as_str() {
            "strict" => "Strict",
            "lax" => "Lax",
            "none" => "None",
            _ => return Err(invalid("same_site", raw, "expected Strict, Lax or None")),
        }),
    };

    let mut out = String::with_capacity(48 + name.len() + value.len());
    out.push_str(name);
    out.push('=');
    out.push_str(value);
    if let Some(e) = expires {
        out.push_str("; Expires=");
        out.push_str(e);
    }
    if let Some(age) = max_age {
        out.push_str("; Max-Age=");
        out.push_str(&age.to_string());
    }
    if let Some(d) = domain {
        out.push_str("; Domain=");
        out.push_str(d);
    }
    if let Some(p) = path {
        out.push_str("; Path=");
        out.push_str(p);
    }
    if secure {
        out.push_str("; Secure");
    }
    if http_only {
        out.push_str("; HttpOnly");
    }
    if let Some(s) = same_site {
        out.push_str("; SameSite=");
        out.push_str(s);
    }
    Ok(out)
}

/// First value of a header in raw transport pairs.
///
/// Accepts either an iterable of pairs (the ASGI scope list) or an object with
/// ``items()`` (``rsgi.Headers``), so the adapters do not need their own scans.
/// Returns ``None`` when the header is absent.
#[pyfunction]
fn first_header(py: Python<'_>, raw: Py<PyAny>, name: &str) -> PyResult<Option<String>> {
    let obj = raw.bind(py);
    let pairs = match obj.call_method0("items") {
        Ok(items) => items,
        Err(err) if err.is_instance_of::<pyo3::exceptions::PyAttributeError>(py) => obj.clone(),
        Err(err) => return Err(err),
    };
    for item in pairs.try_iter()? {
        let item = item?;
        let (key, value) = pair_items(&item)?;
        if name_matches(&key, name)? {
            return Ok(Some(header_text(&value)?));
        }
    }
    Ok(None)
}

/// Build the RFC-combined header dict from raw transport pairs.
///
/// Used where there is no native ``Request`` object — the WebSocket handshake
/// scope. Same combination rules as :attr:`Request.headers`.
#[pyfunction]
fn parse_headers(py: Python<'_>, raw: Py<PyAny>) -> PyResult<PyObject> {
    build_header_dict(py, &raw.bind(py))
}


// ── Response headers ──────────────────────────────────────────────────

/// Encode a validated latin-1 string exactly as Python's ``.encode("latin-1")``.
fn latin1_bytes(value: &str) -> Vec<u8> {
    value.chars().map(|c| c as u8).collect()
}

fn strip_ows(text: &str) -> &str {
    text.trim_matches(|c| c == ' ' || c == '\t')
}

/// Lowercase and validate an RFC 9110 token field name.
fn field_name(raw: &str) -> PyResult<String> {
    let mut name = raw.to_owned();
    name.make_ascii_lowercase();
    if name.is_empty() || !name.as_bytes().iter().all(|b| is_token_byte(*b)) {
        return Err(PyValueError::new_err(format!("Invalid header name: {raw:?}")));
    }
    Ok(name)
}

/// Apply RFC 9110 §5.5 to a field value: latin-1 only, CR/LF/NUL become SP,
/// other CTLs are invalid, leading/trailing OWS is not part of the value.
fn field_value(raw: &str) -> PyResult<String> {
    for ch in raw.chars() {
        if ch as u32 > 0xFF {
            return Err(PyValueError::new_err(format!(
                "Header value is not latin-1 encodable: {raw:?}; encode it (RFC 8187) before setting it"
            )));
        }
    }
    let mut out = String::with_capacity(raw.len());
    for ch in raw.chars() {
        match ch {
            '\0' | '\n' | '\r' => out.push(' '),
            '\t' => out.push('\t'),
            c if (' '..='~').contains(&c) || ('\u{80}'..='\u{FF}').contains(&c) => out.push(c),
            c => {
                return Err(PyValueError::new_err(format!(
                    "Header value contains a control character {c:?}"
                )))
            }
        }
    }
    let trimmed = strip_ows(&out);
    Ok(if trimmed.len() == out.len() {
        out
    } else {
        trimmed.to_owned()
    })
}

impl Headers {
    fn lookup(&self, key: &str) -> Option<String> {
        let mut first: Option<&str> = None;
        let mut combined: Option<String> = None;
        for (existing, value) in &self.lines {
            if existing != key {
                continue;
            }
            if first.is_none() {
                if key == "set-cookie" {
                    return Some(value.clone());
                }
                first = Some(value);
            } else if let Some(joined) = combined.as_mut() {
                joined.push_str(", ");
                joined.push_str(value);
            } else {
                combined = Some(format!("{}, {}", first.unwrap_or_default(), value));
            }
        }
        match (first, combined) {
            (None, _) => None,
            (_, Some(joined)) => Some(joined),
            (Some(value), None) => Some(value.to_owned()),
        }
    }
}

/// Ordered HTTP field lines with duplicates preserved (RFC 9110 §5.3).
///
/// Iteration yields ``(name, value)`` pairs in insertion order. Lookup is
/// case-insensitive and combines repeated fields with ``", "`` except
/// ``set-cookie``, whose lookup returns the first line; ``get_all`` returns
/// every value.
#[pyclass]
pub struct Headers {
    lines: Vec<(String, String)>,
}

#[pymethods]
impl Headers {
    #[new]
    #[pyo3(signature = (items = None))]
    fn new(py: Python<'_>, items: Option<Py<PyAny>>) -> PyResult<Self> {
        let mut headers = Headers { lines: Vec::new() };
        let Some(items) = items else {
            return Ok(headers);
        };
        let obj = items.bind(py);
        if let Ok(source) = obj.extract::<PyRef<Headers>>() {
            headers.lines = source.lines.clone();
            return Ok(headers);
        }
        if let Ok(dict) = obj.downcast::<PyDict>() {
            for (key, value) in dict.iter() {
                headers.add(&header_text(&key)?, &header_text(&value)?)?;
            }
            return Ok(headers);
        }
        match obj.call_method0("items") {
            Ok(pairs) => {
                for item in pairs.try_iter()? {
                    let (key, value) = pair_items(&item?)?;
                    headers.add(&header_text(&key)?, &header_text(&value)?)?;
                }
            }
            Err(err) if err.is_instance_of::<pyo3::exceptions::PyAttributeError>(py) => {
                for item in obj.try_iter()? {
                    let (key, value) = pair_items(&item?)?;
                    headers.add(&header_text(&key)?, &header_text(&value)?)?;
                }
            }
            Err(err) => return Err(err),
        }
        Ok(headers)
    }

    /// Append one field line, keeping any existing line with the same name.
    fn add(&mut self, name: &str, value: &str) -> PyResult<()> {
        self.lines.push((field_name(name)?, field_value(value)?));
        Ok(())
    }

    /// Insert a field line before every existing one.
    fn _prepend(&mut self, name: &str, value: &str) -> PyResult<()> {
        self.lines
            .insert(0, (field_name(name)?, field_value(value)?));
        Ok(())
    }

    fn __setitem__(&mut self, name: &str, value: &str) -> PyResult<()> {
        let key = field_name(name)?;
        let item = (key.clone(), field_value(value)?);
        let mut lines = Vec::with_capacity(self.lines.len());
        let mut replaced = false;
        for (existing, current) in std::mem::take(&mut self.lines) {
            if existing != key {
                lines.push((existing, current));
            } else if !replaced {
                lines.push(item.clone());
                replaced = true;
            }
        }
        if !replaced {
            lines.push(item);
        }
        self.lines = lines;
        Ok(())
    }

    fn __delitem__(&mut self, name: &str) -> PyResult<()> {
        let key = field_name(name)?;
        let before = self.lines.len();
        self.lines.retain(|(existing, _)| *existing != key);
        if self.lines.len() == before {
            return Err(pyo3::exceptions::PyKeyError::new_err(name.to_owned()));
        }
        Ok(())
    }

    fn __contains__(&self, name: &str) -> bool {
        match field_name(name) {
            Ok(key) => self.lines.iter().any(|(existing, _)| *existing == key),
            Err(_) => false,
        }
    }

    fn __len__(&self) -> usize {
        self.lines.len()
    }

    fn __iter__(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let list = PyList::new(py, self.lines.clone())?;
        Ok(list.call_method0("__iter__")?.unbind())
    }

    fn __getitem__(&self, name: &str) -> PyResult<String> {
        let key = field_name(name)?;
        self.lookup(&key)
            .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err(name.to_owned()))
    }

    #[pyo3(signature = (name, default = None))]
    fn get(&self, name: &str, default: Option<String>) -> PyResult<Option<String>> {
        let key = field_name(name)?;
        Ok(self.lookup(&key).or(default))
    }

    fn get_all(&self, name: &str) -> PyResult<Vec<String>> {
        let key = field_name(name)?;
        Ok(self
            .lines
            .iter()
            .filter(|(existing, _)| *existing == key)
            .map(|(_, value)| value.clone())
            .collect())
    }

    fn setdefault(&mut self, name: &str, value: &str) -> PyResult<String> {
        let key = field_name(name)?;
        if let Some(existing) = self.lookup(&key) {
            return Ok(existing);
        }
        let stored = field_value(value)?;
        self.lines.push((key, stored.clone()));
        Ok(stored)
    }

    #[pyo3(signature = (name, default = None))]
    fn pop(&mut self, name: &str, default: Option<String>) -> PyResult<Option<String>> {
        let key = field_name(name)?;
        let value = self.lookup(&key).or(default);
        self.lines.retain(|(existing, _)| *existing != key);
        Ok(value)
    }

    fn keys(&self) -> Vec<String> {
        self.lines.iter().map(|(name, _)| name.clone()).collect()
    }

    fn values(&self) -> Vec<String> {
        self.lines.iter().map(|(_, value)| value.clone()).collect()
    }

    fn items(&self) -> Vec<(String, String)> {
        self.lines.clone()
    }

    /// ASGI wire form: lowercase names, latin-1 bytes.
    fn raw(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let pairs: Vec<(Py<PyBytes>, Py<PyBytes>)> = self
            .lines
            .iter()
            .map(|(name, value)| {
                (
                    PyBytes::new(py, &latin1_bytes(name)).unbind(),
                    PyBytes::new(py, &latin1_bytes(value)).unbind(),
                )
            })
            .collect();
        Ok(PyList::new(py, pairs)?.into_any().unbind())
    }

    /// RSGI wire form: string pairs, order and duplicates kept.
    fn str_pairs(&self) -> Vec<(String, String)> {
        self.lines.clone()
    }

    fn __repr__(&self) -> String {
        format!("Headers({:?})", self.lines)
    }
}

// ── Module ────────────────────────────────────────────────────────────

#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Router>()?;
    m.add_class::<Request>()?;
    m.add_class::<FerronitApp>()?;
    m.add_class::<Headers>()?;
    m.add_function(wrap_pyfunction!(build_set_cookie, m)?)?;
    m.add_function(wrap_pyfunction!(parse_headers, m)?)?;
    m.add_function(wrap_pyfunction!(first_header, m)?)?;
    Ok(())
}
