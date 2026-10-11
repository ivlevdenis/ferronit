"""Security tests — first 10: injection, traversal, header injection, CORS, error handling.

Проверяют реальное поведение Ferronit; тесты, падающие из-за отсутствия
защиты, фиксируются вместе с фиксом (тест — регрессионный страж).
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
    """Прямой ASGI-вызов (контролируем path без нормализации клиента)."""
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
    hdrs = {k.decode(): v.decode() for k, v in start["headers"]}
    body_b = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, hdrs, body_b


class SecUser(Model):
    __table__ = "users_sec"
    id: int = 0
    name: str = ""
    email: str = ""


# ── 1-2. SQL injection ────────────────────────────────────────────────

def test_sql_injection_in_get():
    """Инъекция в id не выполняется — значение уходит bound-параметром."""
    evil = "1 OR '1'='1"
    sql, params = select(SecUser).where(SecUser.c.id == evil).compile()
    assert evil not in sql           # инъекция не попала в текст запроса
    assert params == [evil]          # а осталась значением параметра


def test_sql_injection_in_filter():
    """Инъекция через значение фильтра остаётся значением, не кодом."""
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
    """../../etc/passwd не отдаётся из статики."""
    (tmp_path / "public.txt").write_text("hello")
    app = Ferronit()
    app.mount("/static", StaticFiles(str(tmp_path)))

    status, _, body = await call_app(app, "GET", "/static/../../etc/passwd")
    assert status in (403, 404)
    assert b"root:" not in body

    status, _, body = await call_app(app, "GET", "/static/%2e%2e/%2e%2e/etc/passwd")
    assert status in (403, 404)
    assert b"root:" not in body

    # нормальный файл работает
    status, _, body = await call_app(app, "GET", "/static/public.txt")
    assert status == 200
    assert body == b"hello"


# ── 4. CRLF header injection ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_crlf_header_injection():
    """Заголовок с CRLF из пользовательского ввода не создаёт новые заголовки."""
    app = Ferronit()

    @app.route("/echo")
    def echo(req):
        resp = __import__("ferronit").Response(body=b"ok")
        resp._headers["X-User"] = req.query.get("v", [""])[0]
        return resp

    status, hdrs, _ = await call_app(app, "GET", "/echo", query=b"v=a%0d%0aSet-Cookie:%20evil=1")
    assert status == 200
    assert "Set-Cookie" not in hdrs
    assert hdrs.get("X-User") == "a"  # обрезано до CRLF


# ── 5. XSS — HTML escaping in JSON ────────────────────────────────────

@pytest.mark.asyncio
async def test_xss_json_html_escaping():
    """JSON-ответ не должен содержать сырой <script> (XSS при <script>JSON</script>)."""
    app = Ferronit()

    @app.route("/xss")
    def xss(req):
        return {"user_input": "<script>alert(1)</script>"}

    status, _, body = await call_app(app, "GET", "/xss")
    assert status == 200
    assert b"<script>" not in body
    assert b"\\u003cscript\\u003e" in body or b"\\u003Cscript\\u003E" in body


@pytest.mark.asyncio
async def test_xss_msgspec_struct_html_escaping():
    """msgspec.Struct в ответе (быстрый путь msgspec) тоже экранирует HTML-символы."""
    class Item(msgspec.Struct):
        user_input: str

    app = Ferronit()

    @app.route("/xss-struct")
    def xss_struct(req):
        return Item(user_input="<script>alert(1)</script>&")

    status, _, body = await call_app(app, "GET", "/xss-struct")
    assert status == 200
    assert b"<script>" not in body
    assert b"\\u003cscript\\u003e" in body
    assert b"\\u0026" in body


# ── 6. Deep nesting in JSON body ──────────────────────────────────────

@pytest.mark.asyncio
async def test_deep_nested_json_no_crash():
    """Глубокая вложенность JSON не роняет процесс — валидный HTTP-ответ."""
    app = Ferronit()

    @app.route("/echo", methods=["POST"])
    async def echo(req):
        data = await req.json()
        return {"ok": True, "nested": data is not None}

    deep = b"[" * 300 + b"1" + b"]" * 300
    status, _, _ = await call_app(app, "POST", "/echo", body=deep,
                                  headers=[(b"content-type", b"application/json")])
    assert status in (200, 400)  # либо распарсилось, либо аккуратная ошибка
    assert status != 500


# ── 7. CORS — denied origin ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_cors_denied_origin():
    """Origin вне списка не получает Access-Control-Allow-Origin."""
    app = Ferronit()
    app.use(cors(allow_origins=["https://good.example"]))

    @app.route("/api")
    def api(req):
        return {"ok": True}

    # preflight с запрещённым origin
    status, hdrs, _ = await call_app(
        app, "OPTIONS", "/api",
        headers=[(b"origin", b"https://evil.example"),
                 (b"access-control-request-method", b"GET")],
    )
    assert "Access-Control-Allow-Origin" not in hdrs or hdrs["Access-Control-Allow-Origin"] != "https://evil.example"

    # обычный запрос с запрещённым origin — без ACAO
    status, hdrs, _ = await call_app(
        app, "GET", "/api", headers=[(b"origin", b"https://evil.example")],
    )
    assert "Access-Control-Allow-Origin" not in hdrs

    # разрешённый origin — с ACAO
    status, hdrs, _ = await call_app(
        app, "GET", "/api", headers=[(b"origin", b"https://good.example")],
    )
    assert hdrs.get("Access-Control-Allow-Origin") == "https://good.example"


# ── 8. Debug off hides error details ──────────────────────────────────

@pytest.mark.asyncio
async def test_debug_off_hides_error_details():
    """debug=False: 500 без деталей исключения."""
    app = Ferronit(debug=False)

    @app.route("/boom")
    def boom(req):
        raise RuntimeError("secret-internal-detail-42")

    status, hdrs, body = await call_app(app, "GET", "/boom")
    assert status == 500
    assert b"secret-internal-detail-42" not in body
    assert b"Traceback" not in body


# ── 9. Invalid JSON body → 400, not 500 ───────────────────────────────

@pytest.mark.asyncio
async def test_invalid_json_returns_400():
    """Битый JSON от клиента — 400, не 500."""
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
    """Мусорные/битые пути не роняют роутер."""
    app = Ferronit()

    @app.route("/users/{user_id}")
    def get_user(req):
        return {"id": req.params["user_id"]}

    for bad_path in ("/users/\x00", "/users/%00", "/users/a/b/c", "//users//", "/users/../admin"):
        status, _, _ = await call_app(app, "GET", bad_path)
        assert status in (200, 404), f"{bad_path!r} -> {status}"
