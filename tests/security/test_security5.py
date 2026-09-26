"""Security tests — production hardening batch: security headers, WS origin,
body size limit, rate limiting, anti-fingerprinting."""
import json

import pytest

from velox import Response, Velox
from velox.contrib.ratelimit import rate_limit
from velox.contrib.security import security_headers


async def call_app(app, method: str, path: str, headers: list | None = None,
                   body: bytes = b"", query: bytes = b"", scope_type: str = "http",
                   client: tuple | None = None) -> tuple:
    sent = []

    async def receive():
        if scope_type == "websocket":
            return {"type": "websocket.receive", "text": "ping"}
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(msg):
        sent.append(msg)

    scope = {
        "type": scope_type, "method": method, "path": path,
        "query_string": query, "headers": headers or [],
        "scheme": "http", "server": ("test", 80),
        "client": client or ("1.2.3.4", 1234),
    }
    await app(scope, receive, send)

    if scope_type == "websocket":
        last = sent[-1]
        return last.get("type"), last.get("code"), sent

    start = sent[0]
    status = start["status"]
    hdrs = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    body_b = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, hdrs, body_b


# ── 41. Security headers middleware ───────────────────────────────────

@pytest.mark.asyncio
async def test_security_headers_defaults():
    app = Velox()
    app.use(security_headers())

    @app.route("/api")
    def api(req):
        return {"ok": True}

    status, hdrs, _ = await call_app(app, "GET", "/api")
    assert status == 200
    assert hdrs["x-content-type-options"] == "nosniff"
    assert hdrs["x-frame-options"] == "DENY"
    assert hdrs["referrer-policy"] == "no-referrer"
    assert hdrs["strict-transport-security"].startswith("max-age=31536000")


@pytest.mark.asyncio
async def test_security_headers_options_and_override():
    app = Velox()
    app.use(security_headers(hsts=None, csp="default-src 'self'"))

    @app.route("/api")
    def api(req):
        resp = Response(body=b"ok")
        resp._headers["X-Frame-Options"] = "SAMEORIGIN"  # пользователь переопределяет
        return resp

    status, hdrs, _ = await call_app(app, "GET", "/api")
    assert "strict-transport-security" not in hdrs
    assert hdrs["content-security-policy"] == "default-src 'self'"
    assert hdrs["x-frame-options"] == "SAMEORIGIN"  # setdefault — не перезатирает


# ── 42. WS origin validation ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_ws_origin_allowed_and_denied():
    app = Velox()

    @app.websocket("/ws", origins=["https://good.example"])
    async def ws(conn):
        await conn.accept()
        await conn.send("hi")

    # запрещённый origin → close 1008 (policy violation)
    msg_type, code, _ = await call_app(
        app, "GET", "/ws", scope_type="websocket",
        headers=[(b"origin", b"https://evil.example")],
    )
    assert msg_type == "websocket.close"
    assert code == 1008

    # разрешённый origin → accept
    msg_type, code, sent = await call_app(
        app, "GET", "/ws", scope_type="websocket",
        headers=[(b"origin", b"https://good.example")],
    )
    assert any(m.get("type") == "websocket.accept" for m in sent)

    # без origins — без ограничений
    app2 = Velox()

    @app2.websocket("/open")
    async def open_ws(conn):
        await conn.accept()

    msg_type, _, _ = await call_app(app2, "GET", "/open", scope_type="websocket")
    assert msg_type == "websocket.accept"


# ── 43. max_body_size → 413 ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_max_body_size_413():
    app = Velox(max_body_size=1024)

    @app.route("/upload", methods=["POST"])
    async def upload(req):
        body = await req.body()
        return {"size": len(body)}

    status, _, _ = await call_app(app, "POST", "/upload", body=b"x" * 1025)
    assert status == 413

    status, _, body = await call_app(app, "POST", "/upload", body=b"x" * 1024)
    assert status == 200
    assert json.loads(body)["size"] == 1024


@pytest.mark.asyncio
async def test_max_body_size_chunked():
    """Лимит срабатывает во время чанкованной загрузки, не после."""
    app = Velox(max_body_size=100)

    @app.route("/upload", methods=["POST"])
    async def upload(req):
        await req.body()
        return {"ok": True}

    sent = []

    async def receive():
        return {"type": "http.request", "body": b"x" * 80, "more_body": True}

    async def send(msg):
        sent.append(msg)

    scope = {"type": "http", "method": "POST", "path": "/upload",
             "query_string": b"", "headers": [], "scheme": "http",
             "server": ("t", 80), "client": ("1.2.3.4", 1)}
    await app(scope, receive, send)
    assert sent[0]["status"] == 413


# ── 44. Rate limiting ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rate_limit_429():
    app = Velox()
    app.use(rate_limit(limit=3, window=60.0))

    @app.route("/api")
    def api(req):
        return {"ok": True}

    for i in range(3):
        status, _, _ = await call_app(app, "GET", "/api")
        assert status == 200, f"запрос {i+1} -> {status}"

    status, hdrs, _ = await call_app(app, "GET", "/api")
    assert status == 429
    assert "retry-after" in hdrs


@pytest.mark.asyncio
async def test_rate_limit_per_ip():
    app = Velox()
    app.use(rate_limit(limit=2, window=60.0))

    @app.route("/api")
    def api(req):
        return {"ok": True}

    # разные IP не мешают друг другу
    for ip in ("10.0.0.1", "10.0.0.2"):
        for i in range(2):
            status, _, _ = await call_app(app, "GET", "/api", client=(ip, 1000 + i))
            assert status == 200
        status, _, _ = await call_app(app, "GET", "/api", client=(ip, 9999))
        assert status == 429


@pytest.mark.asyncio
async def test_rate_limit_custom_key():
    app = Velox()
    app.use(rate_limit(limit=2, window=60.0, key=lambda req: req.get_header("x-api-key") or "anon"))

    @app.route("/api")
    def api(req):
        return {"ok": True}

    hdr = [(b"x-api-key", b"key1")]
    assert (await call_app(app, "GET", "/api", headers=hdr))[0] == 200
    assert (await call_app(app, "GET", "/api", headers=hdr))[0] == 200
    assert (await call_app(app, "GET", "/api", headers=hdr))[0] == 429
    # другой ключ — свой лимит
    assert (await call_app(app, "GET", "/api", headers=[(b"x-api-key", b"key2")]))[0] == 200


# ── 45. Anti-fingerprinting ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_server_fingerprint_headers():
    app = Velox()

    @app.route("/api")
    def api(req):
        return {"ok": True}

    status, hdrs, _ = await call_app(app, "GET", "/api")
    assert status == 200
    assert "server" not in hdrs
    assert "x-powered-by" not in hdrs


@pytest.mark.asyncio
async def test_trace_method_not_allowed():
    """TRACE/TRACK — 404, не эхо (защита от TRACE-атак)."""
    app = Velox()

    @app.route("/api")
    def api(req):
        return {"ok": True}

    for method in ("TRACE", "TRACK"):
        status, _, body = await call_app(app, method, "/api")
        assert status == 404, f"{method} -> {status}"
        assert b"Traceback" not in body
