"""Security tests — second batch: static symlink/dotfiles, header injection,
WebSocket hardening, DoS edges, encoding safety."""
import gzip
import json
import os

import pytest

from ferrox import Ferrox, Response
from ferrox.contrib.staticfiles import StaticFiles


async def call_app(app, method: str, path: str, headers: list | None = None,
                   body: bytes = b"", query: bytes = b"", scope_type: str = "http",
                   scope_extra: dict | None = None) -> tuple[int, dict, bytes]:
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
        "scheme": "http", "server": ("test", 80), "client": ("1.2.3.4", 1234),
    }
    if scope_extra:
        scope.update(scope_extra)
    await app(scope, receive, send)

    if scope_type == "websocket":
        last = sent[-1]
        return last.get("type"), last.get("code"), sent

    start = sent[0]
    status = start["status"]
    hdrs = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    body_b = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, hdrs, body_b


# ── 11. Symlink escape from static dir ────────────────────────────────

@pytest.mark.asyncio
async def test_static_symlink_escape(tmp_path):
    """Symlink внутри статики, указывающий наружу, не отдаёт внешние файлы."""
    (tmp_path / "real.txt").write_text("inside")
    target = tmp_path / "target.txt"
    target.write_text("SECRET")
    link = tmp_path / "link.txt"
    os.symlink(target, link)  # link внутри, target внутри — но проверим и наружу

    app = Ferrox()
    app.mount("/static", StaticFiles(str(tmp_path)))

    # symlink на файл ВНЕ статики
    outside = tmp_path.parent / "outside_secret.txt"
    outside.write_text("OUTSIDE_SECRET")
    link2 = tmp_path / "escape.txt"
    os.symlink(outside, link2)

    status, _, body = await call_app(app, "GET", "/static/escape.txt")
    assert status in (403, 404)
    assert b"OUTSIDE_SECRET" not in body

    # обычный файл и symlink внутри — работают
    status, _, body = await call_app(app, "GET", "/static/real.txt")
    assert status == 200 and body == b"inside"


# ── 12. Dotfiles in static ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_static_dotfiles_hidden(tmp_path):
    """.env/.git не должны отдаваться из статики."""
    (tmp_path / ".env").write_text("DATABASE_URL=postgres://secret")
    (tmp_path / ".git").write_text("repo")
    (tmp_path / "index.html").write_text("<h1>ok</h1>")

    app = Ferrox()
    app.mount("/static", StaticFiles(str(tmp_path)))

    status, _, body = await call_app(app, "GET", "/static/.env")
    assert status in (403, 404)
    assert b"DATABASE_URL" not in body

    status, _, body = await call_app(app, "GET", "/static/.git")
    assert status in (403, 404)

    status, _, body = await call_app(app, "GET", "/static/index.html")
    assert status == 200


# ── 13. Content-Type header injection ─────────────────────────────────

@pytest.mark.asyncio
async def test_content_type_header_injection():
    """CRLF в content_type ответа не создаёт новые заголовки."""
    app = Ferrox()

    @app.route("/ct")
    def ct(req):
        return Response(body=b"x", content_type='text/html\r\nX-Injected: yes')

    status, hdrs, _ = await call_app(app, "GET", "/ct")
    assert status == 200
    assert "X-Injected" not in hdrs
    assert hdrs["content-type"] == "text/html"


# ── 14. WebSocket: unknown path → 404 close ───────────────────────────

@pytest.mark.asyncio
async def test_ws_unknown_path_closes():
    app = Ferrox()

    @app.websocket("/ws")
    async def ws(conn):
        await conn.accept()
        await conn.send("hi")

    msg_type, code, _ = await call_app(app, "GET", "/ws/nope", scope_type="websocket")
    assert msg_type == "websocket.close"
    assert code == 404


# ── 15. WebSocket: handler error → close 1011 ─────────────────────────

@pytest.mark.asyncio
async def test_ws_handler_error_closes():
    app = Ferrox()

    @app.websocket("/ws")
    async def ws(conn):
        await conn.accept()
        raise RuntimeError("boom")

    msg_type, code, _ = await call_app(app, "GET", "/ws", scope_type="websocket")
    assert msg_type == "websocket.close"
    assert code == 1011


# ── 16. Gzip response is valid ────────────────────────────────────────

@pytest.mark.asyncio
async def test_gzip_response_valid():
    app = Ferrox()

    @app.route("/data")
    def data(req):
        return {"items": ["a" * 1000] * 20}

    status, hdrs, body = await call_app(
        app, "GET", "/data",
        headers=[(b"accept-encoding", b"gzip")],
    )
    assert status == 200
    assert hdrs.get("content-encoding") == "gzip"
    # тело — валидный gzip
    decoded = json.loads(gzip.decompress(body))
    assert decoded["items"][0] == "a" * 1000


# ── 17. Giant query string doesn't crash ──────────────────────────────

@pytest.mark.asyncio
async def test_giant_query_string_no_crash():
    app = Ferrox()

    @app.route("/search")
    def search(req):
        return {"n": len(req.query)}

    q = b"&".join(b"p%d=%d" % (i, i) for i in range(10000))
    status, _, _ = await call_app(app, "GET", "/search", query=q)
    assert status == 200


# ── 18. Control chars in JSON are escaped ─────────────────────────────

@pytest.mark.asyncio
async def test_control_chars_escaped_in_json():
    """\\x00 и \\x1f в значениях не попадают в ответ сырыми байтами."""
    app = Ferrox()

    @app.route("/ctrl")
    def ctrl(req):
        return {"payload": "\x00\x1f\x7f"}

    status, _, body = await call_app(app, "GET", "/ctrl")
    assert status == 200
    assert b"\x00" not in body
    assert b"\x1f" not in body
    assert b"\\u0000" in body or b"\\u0000" in body
    # парсится обратно корректно
    assert json.loads(body)["payload"] == "\x00\x1f\x7f"


# ── 19. Huge body doesn't crash ───────────────────────────────────────

@pytest.mark.asyncio
async def test_huge_body_no_crash():
    app = Ferrox()

    @app.route("/upload", methods=["POST"])
    async def upload(req):
        body = await req.body()
        return {"size": len(body)}

    big = b"x" * (10 * 1024 * 1024)  # 10 MB
    status, _, body = await call_app(
        app, "POST", "/upload", body=big,
        headers=[(b"content-type", b"application/octet-stream")],
    )
    assert status == 200
    assert json.loads(body)["size"] == len(big)


# ── 20. Method not allowed → clean 404, not 500 ───────────────────────

@pytest.mark.asyncio
async def test_method_not_allowed_no_500():
    app = Ferrox()

    @app.route("/only-get")
    def only_get(req):
        return {"ok": True}

    # POST на GET-маршрут
    status, hdrs, body = await call_app(app, "POST", "/only-get", body=b"{}")
    assert status == 404
    assert b"Traceback" not in body

    # OPTIONS без CORS
    status, hdrs, _ = await call_app(app, "OPTIONS", "/only-get")
    assert status == 404
