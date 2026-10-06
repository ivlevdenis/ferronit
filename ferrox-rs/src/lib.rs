/// Ferrox native core — Router, Request parser, JSON, full ASGI app.
use matchit::Router as MatchitRouter;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};
use serde::ser::Serializer as _;
use std::collections::HashMap;

mod db;

// ── Router ────────────────────────────────────────────────────────────

#[pyclass]
pub struct Router {
    inner: MatchitRouter<PyObject>,
}

#[pymethods]
impl Router {
    #[new]
    fn new() -> Self {
        Router {
            inner: MatchitRouter::new(),
        }
    }

    fn add(&mut self, method: String, pattern: String, handler: PyObject) -> PyResult<()> {
        let key = format!("{} {}", method, pattern);
        self.inner.insert(key, handler).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("Invalid route: {}", e))
        })?;
        Ok(())
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
        let key = format!("{} {}", method, path);
        match self.inner.at(&key) {
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

    fn json(&self, py: Python<'_>) -> PyResult<PyObject> {
        let body = self.body.as_deref().unwrap_or(b"{}");
        serde_json::from_slice::<serde_json::Value>(body)
            .map(|v| json_to_python(py, &v))
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("Invalid JSON: {}", e)))
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
    fn json(data: Bound<'_, PyAny>, status: Option<u16>) -> PyResult<Self> {
        let py = data.py();
        let mut body = Vec::with_capacity(64);
        write_json(py, &data, &mut body)?;
        // XSS guard: escape HTML-significant chars (valid inside JSON strings).
        // `<`, `>`, `&` never appear outside string literals, so a byte-level
        // replace is safe and keeps <script> from breaking out of JSON in HTML.
        escape_html(&mut body);
        Ok(Response {
            body: PyBytes::new(py, &body).into(),
            status: status.unwrap_or(200),
            content_type: "application/json; charset=utf-8".to_string(),
        })
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

fn json_to_python(py: Python<'_>, v: &serde_json::Value) -> PyObject {
    match v {
        serde_json::Value::Null => py.None(),
        serde_json::Value::Bool(b) => b.into_py(py),
        serde_json::Value::Number(n) => {
            if let Some(i) = n.as_i64() { i.into_py(py) }
            else if let Some(f) = n.as_f64() { f.into_py(py) }
            else { n.to_string().into_py(py) }
        }
        serde_json::Value::String(s) => s.into_py(py),
        serde_json::Value::Array(arr) => {
            let list: Vec<PyObject> = arr.iter().map(|v| json_to_python(py, v)).collect();
            PyList::new(py, list).unwrap().into()
        }
        serde_json::Value::Object(obj) => {
            let dict = PyDict::new(py);
            for (k, v) in obj { dict.set_item(k, json_to_python(py, v)).unwrap(); }
            dict.into()
        }
    }
}

/// Escape `<`, `>`, `&` as `\uXXXX` (JSON-safe, XSS-proof inside HTML).
fn escape_html(body: &mut Vec<u8>) {
    let mut i = 0;
    while i < body.len() {
        let repl: &[u8] = match body[i] {
            b'<' => b"\\u003c",
            b'>' => b"\\u003e",
            b'&' => b"\\u0026",
            _ => {
                i += 1;
                continue;
            }
        };
        body.splice(i..i + 1, repl.iter().copied());
        i += repl.len();
    }
}

/// Map a `serde_json` error into a Python `ValueError`.
fn serde_err(error: serde_json::Error) -> PyErr {
    pyo3::exceptions::PyValueError::new_err(format!("Serialize error: {}", error))
}

/// Write one model field as a JSON key/value pair.
fn write_model_field(
    py: Python<'_>,
    obj: &Bound<'_, PyAny>,
    name: &Bound<'_, PyString>,
    out: &mut Vec<u8>,
    first: &mut bool,
) -> PyResult<()> {
    if !*first {
        out.push(b',');
    }
    *first = false;
    serde_json::to_writer(&mut *out, &name.to_str()?).map_err(serde_err)?;
    out.push(b':');
    let value = obj.getattr(name)?;
    write_json(py, &value, out)
}

/// Write a model/dataclass instance as a JSON object, without an intermediate dict.
///
/// A `rawmodel.Model` exposes `__columns__` (field names in `SELECT *` order);
/// a plain dataclass exposes `__dataclass_fields__` (name → field, in declaration
/// order). Either way values are read with `getattr` and written recursively.
/// Returns `false` when `obj` is neither, so the caller can fall back.
fn write_model(py: Python<'_>, obj: &Bound<'_, PyAny>, out: &mut Vec<u8>) -> PyResult<bool> {
    let ty = obj.get_type();
    let columns = if let Ok(columns) = ty.getattr("__columns__") {
        columns
    } else if let Ok(fields) = ty.getattr("__dataclass_fields__") {
        fields
    } else {
        return Ok(false);
    };

    let mut first = true;
    if let Ok(names) = columns.downcast::<PyTuple>() {
        out.push(b'{');
        for name in names.iter() {
            if let Ok(name) = name.downcast::<PyString>() {
                write_model_field(py, obj, name, out, &mut first)?;
            }
        }
    } else if let Ok(fields) = columns.downcast::<PyDict>() {
        out.push(b'{');
        for (name, _) in fields.iter() {
            if let Ok(name) = name.downcast::<PyString>() {
                write_model_field(py, obj, name, out, &mut first)?;
            }
        }
    } else {
        return Ok(false);
    }
    out.push(b'}');
    Ok(true)
}

/// Serialise a Python object to JSON directly into a byte buffer —
/// no intermediate `serde_json::Value` (one pass, fewer allocations).
///
/// Dispatch uses `downcast` type checks, not trial `extract`: a failed `extract`
/// raises and clears a Python exception, which dominated the cost on wide
/// payloads (checked before the switch: ~1.2 ms just to encode 1000 dict rows).
/// Model/dataclass instances are written straight from their fields, so a handler
/// may return `models` instead of building dicts by hand.
fn write_json(py: Python<'_>, obj: &Bound<'_, PyAny>, out: &mut Vec<u8>) -> PyResult<()> {
    if obj.is_none() {
        out.extend_from_slice(b"null");
        return Ok(());
    }
    // bool раньше int: PyBool — подкласс PyInt
    if let Ok(b) = obj.downcast::<PyBool>() {
        out.extend_from_slice(if b.is_true() { b"true" } else { b"false" });
        return Ok(());
    }
    if let Ok(i) = obj.downcast::<PyInt>() {
        if let Ok(v) = i.extract::<i64>() {
            serde_json::Serializer::new(&mut *out)
                .serialize_i64(v)
                .map_err(serde_err)?;
        } else if let Ok(v) = i.extract::<u64>() {
            serde_json::Serializer::new(&mut *out)
                .serialize_u64(v)
                .map_err(serde_err)?;
        } else if let Ok(v) = i.extract::<f64>() {
            serde_json::Serializer::new(&mut *out)
                .serialize_f64(v)
                .map_err(serde_err)?;
        } else {
            out.extend_from_slice(b"null");
        }
        return Ok(());
    }
    if let Ok(f) = obj.downcast::<PyFloat>() {
        match serde_json::Number::from_f64(f.value()) {
            Some(n) => serde_json::Serializer::new(&mut *out)
                .serialize_f64(n.as_f64().unwrap())
                .map_err(serde_err)?,
            None => out.extend_from_slice(b"null"),
        }
        return Ok(());
    }
    if let Ok(s) = obj.downcast::<PyString>() {
        serde_json::to_writer(&mut *out, &s.to_str()?).map_err(serde_err)?;
        return Ok(());
    }
    if let Ok(list) = obj.downcast::<PyList>() {
        out.push(b'[');
        let mut first = true;
        for item in list.iter() {
            if !first {
                out.push(b',');
            }
            first = false;
            write_json(py, &item, out)?;
        }
        out.push(b']');
        return Ok(());
    }
    if let Ok(tuple) = obj.downcast::<PyTuple>() {
        out.push(b'[');
        let mut first = true;
        for item in tuple.iter() {
            if !first {
                out.push(b',');
            }
            first = false;
            write_json(py, &item, out)?;
        }
        out.push(b']');
        return Ok(());
    }
    if let Ok(dict) = obj.downcast::<PyDict>() {
        out.push(b'{');
        let mut first = true;
        for (k, v) in dict.iter() {
            if !first {
                out.push(b',');
            }
            first = false;
            let key: String = k.extract()?;
            serde_json::to_writer(&mut *out, &key).map_err(serde_err)?;
            out.push(b':');
            write_json(py, &v, out)?;
        }
        out.push(b'}');
        return Ok(());
    }
    if write_model(py, obj, out)? {
        return Ok(());
    }
    let s = obj.str()?.to_string();
    serde_json::to_writer(&mut *out, &s).map_err(serde_err)?;
    Ok(())
}

// ── ASGI App ──────────────────────────────────────────────────────────

/// Full Rust-native ASGI application.
/// Owns the router, receives scope/receive/send from Python, dispatches.
#[pyclass]
pub struct FerroxApp {
    router: Router,
    cors_origins: Vec<String>,
    cors_methods: Option<String>,
    cors_headers: Option<String>,
    cors_max_age: Option<String>,
}

#[pymethods]
impl FerroxApp {
    #[new]
    fn new() -> Self {
        FerroxApp {
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

    /// Handle one HTTP request — routing + handler call + response build.
    /// Returns the Response object (status, body, content_type).
    fn handle_request(
        &mut self,
        py: Python<'_>,
        method: String,
        path: String,
        query_string: String,
        raw_headers: Vec<(Vec<u8>, Vec<u8>)>,
        body: Vec<u8>,
    ) -> PyResult<Response> {
        let result = self.router.lookup(py, &method, &path)?;
        let (handler, params) = match result {
            Some(x) => x,
            None => {
                return Ok(Response::text("Not Found".to_string(), Some(404), py));
            }
        };

        let scope = PyDict::new(py);
        scope.set_item("method", &method)?;
        scope.set_item("path", &path)?;
        scope.set_item("query_string", PyBytes::new(py, query_string.as_bytes()))?;

        let hdr_list = PyList::empty(py);
        for (k, v) in &raw_headers {
            hdr_list.append((PyBytes::new(py, k), PyBytes::new(py, v)))?;
        }
        scope.set_item("headers", hdr_list)?;

        let params_dict = PyDict::new(py);
        if let Some(params) = params {
            for (k, v) in &params {
                params_dict.set_item(k, v)?;
            }
        }
        scope.set_item("route_params", params_dict)?;

        // Call Python handler with scope dict
        let result = handler.bind(py).call1((scope,))?;

        // Build Rust response from Python result
        if let Ok(dict) = result.downcast::<PyDict>() {
            Response::json(result, Some(200))
        } else if let Ok(s) = result.extract::<String>() {
            Ok(Response::text(s, Some(200), py))
        } else {
            Ok(Response::text(result.str()?.to_string(), Some(200), py))
        }
    }
}

// ── Module ────────────────────────────────────────────────────────────

#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Router>()?;
    m.add_class::<Request>()?;
    m.add_class::<Response>()?;
    m.add_class::<FerroxApp>()?;

    // Вложенный модуль данных: `ferrox._core.db` (алиасы `ferrox_core.db`, `ferrox_db`).
    let db_module = PyModule::new(m.py(), "db")?;
    db::register(&db_module)?;
    m.add_submodule(&db_module)?;
    // add_submodule ставит атрибут, но не sys.modules — регистрируем, чтобы
    // `from ferrox._core.db import ...` тоже работал.
    m.py()
        .import("sys")?
        .getattr("modules")?
        .set_item("ferrox._core.db", &db_module)?;
    Ok(())
}
