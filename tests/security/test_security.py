"""Security tests — first 10: injection, traversal, header injection, CORS, error handling.

They check Ferronit's real behavior; tests that fail due to missing protection
are committed together with the fix (the test is a regression guard).
"""
import msgspec
import pytest

from ferronit import Ferronit
from ferronit.contrib.cors import cors
from ferronit.contrib.rawmodel import Model, select
from ferronit.contrib.staticfiles import StaticFiles

# ── helpers ───────────────────────────────────────────────────────────

async def call_app(app, method: str, path: str, headers: list | None = None,
                   body: bytes = b"", query: bytes = b"") -> tuple[int, dict, bytes]:
    """Direct ASGI call (path controlled without client normalization)."""
    sent = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(msg):
        sent.append(msg)

    scope = {
        "type": "http", "method": method, "path": path,
        "query_string": query, "headers": headers or [],
        "scheme": "http", "server": ("test", 80), "client": ("1.2.3.4", 1234),
    }
    await app(scope, receive, send)
    start = sent[0]
    status = start["status"]
    hdrs = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    body_b = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, hdrs, body_b


class SecUser(Model):
    __table__ = "users_sec"
    id: int = 0
    name: str = ""
    email: str = ""


# ── 1-2. SQL injection ────────────────────────────────────────────────

def test_sql_injection_in_get():
    """An injection in the id does not execute — the value goes as a bound parameter."""
    evil = "1 OR '1'='1"
    sql, params = select(SecUser).where(SecUser.c.id == evil).compile()
    assert evil not in sql           # the injection did not reach the query text
    assert params == [evil]          # it stayed a parameter value


def test_sql_injection_in_filter():
    """An injection through a filter value stays a value, not code."""
    evil = "' OR '1'='1"
    sql, params = select(SecUser).where(SecUser.c.name == evil).compile()
    assert evil not in sql
    assert params == [evil]

    drop = "admin'; DROP TABLE users_sec; --"
    sql2, params2 = select(SecUser).where(SecUser.c.name == drop).compile()
    assert "DROP TABLE" not in sql2
    assert params2 == [drop]


# ── 3. Path traversal (static files) ──────────────────────────────────

@pytest.mark.asyncio
async def test_static_path_traversal(tmp_path):
    """../../etc/passwd is not served from static files."""
    (tmp_path / "public.txt").write_text("hello")
    app = Ferronit()
    app.mount("/static", StaticFiles(str(tmp_path)))

    status, _, body = await call_app(app, "GET", "/static/../../etc/passwd")
    assert status in (403, 404)
    assert b"root:" not in body

    status, _, body = await call_app(app, "GET", "/static/%2e%2e/%2e%2e/etc/passwd")
    assert status in (403, 404)
    assert b"root:" not in body

    # a normal file works
    status, _, body = await call_app(app, "GET", "/static/public.txt")
    assert status == 200
    assert body == b"hello"


# ── 4. CRLF header injection ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_crlf_header_injection():
    """A header with CRLF from user input does not create new headers."""
    app = Ferronit()

    @app.route("/echo")
    async def echo(req):
        resp = __import__("ferronit").Response(body=b"ok")
        resp._headers["X-User"] = req.query.get("v", [""])[0]
        return resp

    status, hdrs, _ = await call_app(app, "GET", "/echo", query=b"v=a%0d%0aSet-Cookie:%20evil=1")
    assert status == 200
    assert "set-cookie" not in hdrs
    value = hdrs["x-user"]  # CRLF replaced with SP, no second line appeared
    assert "\r" not in value and "\n" not in value
    assert value.startswith("a")


# ── 5. XSS — HTML escaping in JSON ────────────────────────────────────

@pytest.mark.asyncio
async def test_xss_json_html_escaping():
    """A JSON response must not contain raw <script> (XSS via <script>JSON</script>)."""
    app = Ferronit()

    @app.route("/xss")
    async def xss(req):
        return {"user_input": "<script>alert(1)</script>"}

    status, _, body = await call_app(app, "GET", "/xss")
    assert status == 200
    assert b"<script>" not in body
    assert b"\\u003cscript\\u003e" in body or b"\\u003Cscript\\u003E" in body


@pytest.mark.asyncio
async def test_xss_msgspec_struct_html_escaping():
    """A msgspec.Struct in the response (the fast msgspec path) escapes HTML chars too."""
    class Item(msgspec.Struct):
        user_input: str

    app = Ferronit()

    @app.route("/xss-struct")
    async def xss_struct(req):
        return Item(user_input="<script>alert(1)</script>&")

    status, _, body = await call_app(app, "GET", "/xss-struct")
    assert status == 200
    assert b"<script>" not in body
    assert b"\\u003cscript\\u003e" in body
    assert b"\\u0026" in body


# ── 6. Deep nesting in JSON body ──────────────────────────────────────

@pytest.mark.asyncio
async def test_deep_nested_json_no_crash():
    """Deep JSON nesting does not crash the process — a valid HTTP response."""
    app = Ferronit()

    @app.route("/echo", methods=["POST"])
    async def echo(req):
        data = await req.json()
        return {"ok": True, "nested": data is not None}

    deep = b"[" * 300 + b"1" + b"]" * 300
    status, _, _ = await call_app(app, "POST", "/echo", body=deep,
                                  headers=[(b"content-type", b"application/json")])
    assert status in (200, 400)  # either it parsed, or a clean error
    assert status != 500


# ── 7. CORS — denied origin ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_cors_denied_origin():
    """An origin outside the list does not get Access-Control-Allow-Origin."""
    app = Ferronit()
    app.use(cors(allow_origins=["https://good.example"]))

    @app.route("/api")
    async def api(req):
        return {"ok": True}

    # preflight with a disallowed origin
    status, hdrs, _ = await call_app(
        app, "OPTIONS", "/api",
        headers=[(b"origin", b"https://evil.example"),
                 (b"access-control-request-method", b"GET")],
    )
    assert "access-control-allow-origin" not in hdrs or hdrs["access-control-allow-origin"] != "https://evil.example"

    # a regular request with a disallowed origin — no ACAO
    status, hdrs, _ = await call_app(
        app, "GET", "/api", headers=[(b"origin", b"https://evil.example")],
    )
    assert "access-control-allow-origin" not in hdrs

    # an allowed origin — with ACAO
    status, hdrs, _ = await call_app(
        app, "GET", "/api", headers=[(b"origin", b"https://good.example")],
    )
    assert hdrs.get("access-control-allow-origin") == "https://good.example"


# ── 8. Debug off hides error details ──────────────────────────────────

@pytest.mark.asyncio
async def test_debug_off_hides_error_details():
    """debug=False: 500 without exception details."""
    app = Ferronit(debug=False)

    @app.route("/boom")
    async def boom(req):
        raise RuntimeError("secret-internal-detail-42")

    status, hdrs, body = await call_app(app, "GET", "/boom")
    assert status == 500
    assert b"secret-internal-detail-42" not in body
    assert b"Traceback" not in body


# ── 9. Invalid JSON body → 400, not 500 ───────────────────────────────

@pytest.mark.asyncio
async def test_invalid_json_returns_400():
    """Broken JSON from the client — 400, not 500."""
    app = Ferronit()

    @app.route("/echo", methods=["POST"])
    async def echo(req):
        data = await req.json()
        return {"got": data}

    status, hdrs, _ = await call_app(app, "POST", "/echo", body=b"{not json!!",
                                     headers=[(b"content-type", b"application/json")])
    assert status == 400


# ── 10. Malformed path (null byte) doesn't crash ──────────────────────

@pytest.mark.asyncio
async def test_malformed_path_no_crash():
    """Garbage/broken paths do not crash the router."""
    app = Ferronit()

    @app.route("/users/{user_id}")
    async def get_user(req):
        return {"id": req.params["user_id"]}

    for bad_path in ("/users/\x00", "/users/%00", "/users/a/b/c", "//users//", "/users/../admin"):
        status, _, _ = await call_app(app, "GET", bad_path)
        assert status in (200, 404), f"{bad_path!r} -> {status}"
