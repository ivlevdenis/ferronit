"""Security tests — fourth batch: content negotiation, trace spoofing,
header floods, chunked body, query decoding, auth middleware pattern."""
import json

import pytest

from ferronit import Ferronit, Response


async def call_app(app, method: str, path: str, headers: list | None = None,
                   body: bytes = b"", query: bytes = b"", chunks: list[bytes] | None = None,
                   scope_type: str = "http") -> tuple:
    sent = []
    chunk_iter = iter(chunks or [body] if chunks is not None else [body])

    async def receive():
        if scope_type == "websocket":
            return {"type": "websocket.receive", "text": "ping"}
        try:
            chunk = next(chunk_iter)
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.request", "body": chunk, "more_body": True}

    async def send(msg):
        sent.append(msg)

    scope = {
        "type": scope_type, "method": method, "path": path,
        "query_string": query, "headers": headers or [],
        "scheme": "http", "server": ("test", 80), "client": ("1.2.3.4", 1234),
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


# ── 31. gzip;q=0 disables compression ─────────────────────────────────

@pytest.mark.asyncio
async def test_gzip_q0_disables_compression():
    """accept-encoding: gzip;q=0 — клиент не хочет gzip, не сжимаем."""
    app = Ferronit()

    @app.route("/data")
    def data(req):
        return {"items": ["x" * 500] * 10}

    status, hdrs, body = await call_app(
        app, "GET", "/data", headers=[(b"accept-encoding", b"gzip;q=0")],
    )
    assert status == 200
    assert "content-encoding" not in hdrs

    # gzip;q=1 — сжимаем
    status, hdrs, body = await call_app(
        app, "GET", "/data", headers=[(b"accept-encoding", b"gzip;q=1")],
    )
    assert hdrs.get("content-encoding") == "gzip"

    # сложный список: br;q=1, gzip;q=0.5 — сжимаем (q > 0)
    status, hdrs, body = await call_app(
        app, "GET", "/data", headers=[(b"accept-encoding", b"br;q=1, gzip;q=0.5")],
    )
    assert hdrs.get("content-encoding") == "gzip"


# ── 32. X-Trace-Id not reflected without trace middleware ─────────────

@pytest.mark.asyncio
async def test_trace_id_not_reflected_without_middleware():
    """Пользовательский X-Trace-Id не попадает в ответ без trace middleware."""
    app = Ferronit()

    @app.route("/api")
    def api(req):
        return {"ok": True}

    status, hdrs, _ = await call_app(
        app, "GET", "/api", headers=[(b"x-trace-id", b"spoofed-123")],
    )
    assert status == 200
    assert "x-trace-id" not in hdrs


# ── 33. Header flood (1000 headers) doesn't crash ─────────────────────

@pytest.mark.asyncio
async def test_header_flood_no_crash():
    app = Ferronit()

    @app.route("/api")
    def api(req):
        return {"n": len(req.headers)}

    hdrs = [(b"x-h%d" % i, b"v%d" % i) for i in range(1000)]
    hdrs.append((b"host", b"test"))
    status, _, body = await call_app(app, "GET", "/api", headers=hdrs)
    assert status == 200
    assert json.loads(body)["n"] == 1001


# ── 34. Huge header value (1MB) doesn't crash ─────────────────────────

@pytest.mark.asyncio
async def test_huge_header_no_crash():
    app = Ferronit()

    @app.route("/api")
    def api(req):
        return {"len": len(req.headers.get("x-big", ""))}

    status, _, body = await call_app(
        app, "GET", "/api", headers=[(b"x-big", b"a" * (1024 * 1024))],
    )
    assert status == 200
    assert json.loads(body)["len"] == 1024 * 1024


# ── 35. Chunked body assembled correctly ──────────────────────────────

@pytest.mark.asyncio
async def test_chunked_body_assembled():
    app = Ferronit()

    @app.route("/echo", methods=["POST"])
    async def echo(req):
        body = await req.body()
        return {"size": len(body), "sha": sum(body)}

    chunks = [b"part1-", b"part2-", b"part3"]
    status, _, body = await call_app(app, "POST", "/echo", chunks=chunks)
    assert status == 200
    data = json.loads(body)
    assert data["size"] == len(b"part1-part2-part3")


# ── 36. Top-level JSON values (null, number, array) ───────────────────

@pytest.mark.asyncio
async def test_json_top_level_values():
    app = Ferronit()

    @app.route("/echo", methods=["POST"])
    async def echo(req):
        data = await req.json()
        return {"got": data}

    for payload in (b"null", b"42", b"[1,2,3]", b'"str"', b"true"):
        status, _, body = await call_app(
            app, "POST", "/echo", body=payload,
            headers=[(b"content-type", b"application/json")],
        )
        assert status == 200, f"{payload!r} -> {status}"
        assert json.loads(body)["got"] == json.loads(payload)


# ── 37. Broken query pairs don't crash ────────────────────────────────

@pytest.mark.asyncio
async def test_broken_query_pairs_no_crash():
    app = Ferronit()

    @app.route("/q")
    def q(req):
        return {"n": len(req.query)}

    for raw in (b"a=1&b&c", b"=x", b"a==1", b"%zz=%", b"&&&", b"a=%"):
        status, _, _ = await call_app(app, "GET", "/q", query=raw)
        assert status == 200, f"{raw!r} -> {status}"


# ── 38. UTF-8 query decoding (%D0%BF%D1%80...) ────────────────────────

@pytest.mark.asyncio
async def test_utf8_query_decoding():
    """Percent-encoded UTF-8 декодируется в нормальную строку, не в кракозябры."""
    app = Ferronit()

    @app.route("/search")
    def search(req):
        return {"q": req.query.get("q", [""])[0]}

    status, _, body = await call_app(
        app, "GET", "/search",
        query=b"q=%D0%BF%D1%80%D0%B8%D0%B2%D0%B5%D1%82",
    )
    assert status == 200
    assert json.loads(body)["q"] == "привет"

    # '+' — пробел
    status, _, body = await call_app(app, "GET", "/search", query=b"q=hello+world")
    assert json.loads(body)["q"] == "hello world"


# ── 39. Very long path (64KB) → clean 404 ─────────────────────────────

@pytest.mark.asyncio
async def test_very_long_path_no_crash():
    app = Ferronit()

    @app.route("/")
    def home(req):
        return {"ok": True}

    long_path = "/" + "a" * (64 * 1024)
    status, _, body = await call_app(app, "GET", long_path)
    assert status == 404
    assert b"Traceback" not in body


# ── 40. Auth middleware can short-circuit (401) ───────────────────────

@pytest.mark.asyncio
async def test_auth_middleware_short_circuit():
    """Middleware-паттерн авторизации: без токена — 401, хендлер не вызывается."""
    app = Ferronit()

    async def auth(req, next_handler):
        token = req.get_header("authorization")
        if token != "Bearer valid":
            return Response(body=b"unauthorized", status=401)
        result = next_handler(req)
        if hasattr(result, "__await__"):
            return await result
        return result

    app.use(auth)
    called = []

    @app.route("/private")
    def private(req):
        called.append(True)
        return {"secret": "data"}

    status, _, body = await call_app(app, "GET", "/private")
    assert status == 401
    assert called == []  # хендлер не выполнялся

    status, _, body = await call_app(
        app, "GET", "/private", headers=[(b"authorization", b"Bearer valid")],
    )
    assert status == 200
    assert called == [True]
