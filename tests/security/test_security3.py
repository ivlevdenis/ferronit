"""Security tests — third batch: JSON parsing attacks, path param decoding,
middleware errors, WebSocket edges."""
import json

import pytest

from velox import Velox


async def call_app(app, method: str, path: str, headers: list | None = None,
                   body: bytes = b"", query: bytes = b"", scope_type: str = "http",
                   scope_extra: dict | None = None) -> tuple:
    sent = []

    async def receive():
        if scope_type == "websocket":
            return {"type": "websocket.receive", "text": "{not json"}
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


def make_json_app():
    app = Velox()

    @app.route("/echo", methods=["POST"])
    async def echo(req):
        data = await req.json()
        return {"got": data}

    return app


# ── 21. JSON numeric attacks (1e999, huge ints) ───────────────────────

@pytest.mark.asyncio
async def test_json_numeric_attacks_no_crash():
    """1e999 и 1000-значные числа не роняют парсер — аккуратная ошибка."""
    app = make_json_app()

    for payload in (b"1e999", b"9" * 1000, b"-9" * 500 + b"9"):
        status, _, _ = await call_app(
            app, "POST", "/echo", body=payload,
            headers=[(b"content-type", b"application/json")],
        )
        assert status in (200, 400), f"{payload[:20]!r} -> {status}"


# ── 22. JSON prototype pollution ──────────────────────────────────────

@pytest.mark.asyncio
async def test_json_prototype_pollution_safe():
    """__proto__/constructor в JSON — обычные ключи, никакого поллишена."""
    app = make_json_app()

    body = json.dumps({"__proto__": {"polluted": True}, "constructor": {"x": 1}}).encode()
    status, _, resp_body = await call_app(
        app, "POST", "/echo", body=body,
        headers=[(b"content-type", b"application/json")],
    )
    assert status == 200
    data = json.loads(resp_body)["got"]
    assert data["__proto__"]["polluted"] is True  # это просто данные
    assert data["constructor"]["x"] == 1
    # ничего не «утекло» в глобальный объект
    assert not hasattr({}, "polluted")


# ── 23. Duplicate JSON keys ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_json_duplicate_keys_last_wins():
    """Повторяющиеся ключи: последний побеждает, без краха."""
    app = make_json_app()

    status, _, resp_body = await call_app(
        app, "POST", "/echo", body=b'{"a": 1, "a": 2, "a": 3}',
        headers=[(b"content-type", b"application/json")],
    )
    assert status == 200
    assert json.loads(resp_body)["got"]["a"] == 3


# ── 24. Invalid UTF-8 in body → 400 ───────────────────────────────────

@pytest.mark.asyncio
async def test_invalid_utf8_body_400():
    """Бинарный мусор вместо JSON — 400, не 500."""
    app = make_json_app()

    status, _, _ = await call_app(
        app, "POST", "/echo", body=b"\xff\xfe\x00\x01",
        headers=[(b"content-type", b"application/json")],
    )
    assert status == 400


# ── 25. Empty body + json() → clean error ─────────────────────────────

@pytest.mark.asyncio
async def test_empty_body_json_clean_error():
    """json() без тела — 400 (не 500, не краш)."""
    app = make_json_app()

    status, hdrs, body = await call_app(app, "POST", "/echo", body=b"")
    assert status == 400
    assert b"Traceback" not in body


# ── 26. json() ignores content-type but validates body ────────────────

@pytest.mark.asyncio
async def test_json_wrong_content_type_still_validates():
    """Даже с text/plain заголовком битый body → 400."""
    app = make_json_app()

    status, _, _ = await call_app(
        app, "POST", "/echo", body=b"not json",
        headers=[(b"content-type", b"text/plain")],
    )
    assert status == 400

    status, _, _ = await call_app(
        app, "POST", "/echo", body=b'{"ok": 1}',
        headers=[(b"content-type", b"text/plain")],
    )
    assert status == 200


# ── 27. Path params: decoded values, raw %-sequences don't crash ──────

@pytest.mark.asyncio
async def test_path_params_decoding():
    """ASGI path уже декодирован — параметр приходит как есть, без двойного декода."""
    app = Velox()

    @app.route("/users/{user_id}")
    def get_user(req):
        return {"id": req.params["user_id"]}

    # декодированный scope path (как отдают uvicorn/granian)
    status, _, body = await call_app(app, "GET", "/users/user name")
    assert status == 200
    assert json.loads(body)["id"] == "user name"

    # сырые %-последовательности в несуществующих маршрутах — 404, не краш
    for bad in ("/users/%FF", "/users/%00", "/users/a%2Fb"):
        status, _, _ = await call_app(app, "GET", bad)
        assert status in (200, 404), f"{bad!r} -> {status}"


# ── 28. Middleware error → 500 generic ────────────────────────────────

@pytest.mark.asyncio
async def test_middleware_error_500_generic():
    """Ошибка в middleware — 500 без внутренностей."""
    app = Velox()

    def evil_mw(req, next_handler):
        raise RuntimeError("middleware-secret-xyz")

    app.use(evil_mw)

    @app.route("/ok")
    def ok(req):
        return {"ok": True}

    status, _, body = await call_app(app, "GET", "/ok")
    assert status == 500
    assert b"middleware-secret-xyz" not in body
    assert b"Traceback" not in body


# ── 29. Unicode paths don't crash ─────────────────────────────────────

@pytest.mark.asyncio
async def test_unicode_paths_no_crash():
    app = Velox()

    @app.route("/users/{user_id}")
    def get_user(req):
        return {"id": req.params["user_id"]}

    status, _, body = await call_app(app, "GET", "/users/привет")
    assert status == 200
    assert json.loads(body)["id"] == "привет"

    status, _, _ = await call_app(app, "GET", "/несуществующий/маршрут")
    assert status == 404


# ── 30. WS receive_json with broken JSON → close 1011 ─────────────────

@pytest.mark.asyncio
async def test_ws_receive_json_broken_closes():
    app = Velox()

    @app.websocket("/ws")
    async def ws(conn):
        await conn.accept()
        await conn.receive_json()

    msg_type, code, _ = await call_app(app, "GET", "/ws", scope_type="websocket")
    assert msg_type == "websocket.close"
    assert code == 1011
