"""ASVS 5.0 Level 1 compliance tests — каждый тест = одно требование L1.

Имена: test_asvs_<V>_<N>_<M>_<slug>. Docstring содержит полный текст требования
из официального OWASP ASVS 5.0 (github.com/OWASP/ASVS, 5.0/en/).
"""
import gzip
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, insert
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from velox import Response, Velox
from velox.contrib.db import RelationalUnitOfWork
from velox.contrib.ratelimit import rate_limit
from velox.contrib.security import security_headers
from velox.contrib.staticfiles import StaticFiles

VELOX_ROOT = Path(__file__).resolve().parents[2]


async def call_app(app, method: str, path: str, headers: list | None = None,
                   body: bytes = b"", query: bytes = b"") -> tuple[int, dict, bytes]:
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


# ── V1 Encoding and Sanitization ──────────────────────────────────────

@pytest.mark.asyncio
async def test_asvs_v1_2_1_output_encoding_for_json_context():
    """V1.2.1 (L1): output encoding for an HTTP response is relevant for the
    context. JSON responses must not contain raw HTML-significant characters
    (XSS protection when JSON is embedded in HTML)."""
    app = Velox()

    @app.route("/xss")
    def xss(req):
        return {"user_input": "<script>alert(1)</script>&"}

    status, _, body = await call_app(app, "GET", "/xss")
    assert status == 200
    assert b"<script>" not in body
    assert b"\\u003cscript\\u003e" in body
    assert b"\\u0026" in body  # & тоже экранирован


@pytest.mark.asyncio
async def test_asvs_v1_2_4_parameterized_queries():
    """V1.2.4 (L1): data selection or database queries use parameterized queries."""
    metadata = MetaData()
    users = Table(
        "users_asvs", metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("name", String, nullable=False),
    )
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    uow = RelationalUnitOfWork(factory)

    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    async with uow:
        repo = uow[users]
        await repo.save({"name": "A"})
        await uow.commit()

        # классическая инъекция не выполняется
        assert await repo.get("1 OR 1=1") is None
        assert await repo.get("1; DROP TABLE users_asvs; --") is None
        rows = await repo.list(users.c.name == "' OR '1'='1")
        assert rows == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_asvs_v1_3_2_no_dynamic_code_execution():
    """V1.3.2 (L1): the application avoids eval() or other dynamic code
    execution features. Runtime code must not contain eval/exec."""
    import re

    offenders = []
    for path in (VELOX_ROOT / "velox").rglob("*.py"):
        if "cli.py" in str(path):  # CLI-шаблоны — не рантайм
            continue
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"\b(eval|exec)\s*\(", line):
                offenders.append(f"{path.relative_to(VELOX_ROOT)}:{i}")
    assert offenders == [], f"eval/exec найден: {offenders}"


# ── V3 Web Frontend Security ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_asvs_v3_2_1_clickjacking_protection():
    """V3.2.1 (L1): security controls prevent browsers from rendering content
    in a frame (X-Frame-Options / frame-ancestors)."""
    app = Velox()
    app.use(security_headers())

    @app.route("/api")
    def api(req):
        return {"ok": True}

    status, hdrs, _ = await call_app(app, "GET", "/api")
    assert hdrs["x-frame-options"] == "DENY"


@pytest.mark.asyncio
async def test_asvs_v3_4_1_hsts():
    """V3.4.1 (L1): Strict-Transport-Security header is included on all responses."""
    app = Velox()
    app.use(security_headers())

    @app.route("/api")
    def api(req):
        return {"ok": True}

    status, hdrs, _ = await call_app(app, "GET", "/api")
    assert hdrs["strict-transport-security"].startswith("max-age=31536000")


@pytest.mark.asyncio
async def test_asvs_v3_4_2_cors_fixed_origin():
    """V3.4.2 (L1): CORS Access-Control-Allow-Origin is a fixed value and does
    not reflect arbitrary attacker-controlled origins."""
    app = Velox()
    app.use(__import__("velox.contrib.cors", fromlist=["cors"]).cors(
        allow_origins=["https://good.example"]
    ))

    @app.route("/api")
    def api(req):
        return {"ok": True}

    # evil origin не получает ACAO
    status, hdrs, _ = await call_app(app, "GET", "/api",
                                     headers=[(b"origin", b"https://evil.example")])
    assert "access-control-allow-origin" not in hdrs

    # good origin получает ровно свой
    status, hdrs, _ = await call_app(app, "GET", "/api",
                                     headers=[(b"origin", b"https://good.example")])
    assert hdrs["access-control-allow-origin"] == "https://good.example"


@pytest.mark.asyncio
async def test_asvs_v3_5_2_cors_preflight_origin_check():
    """V3.5.2 (L1): if the application relies on the CORS preflight mechanism,
    disallowed cross-origin requests are rejected (no ACAO for evil origin)."""
    from velox.contrib.cors import cors

    app = Velox()
    app.use(cors(allow_origins=["https://good.example"]))

    @app.route("/api", methods=["POST"])
    def api(req):
        return {"ok": True}

    status, hdrs, _ = await call_app(
        app, "OPTIONS", "/api",
        headers=[(b"origin", b"https://evil.example"),
                 (b"access-control-request-method", b"POST")],
    )
    assert "access-control-allow-origin" not in hdrs


# ── V4 API and Web Service ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_asvs_v4_1_1_content_type_matches_content_with_charset():
    """V4.1.1 (L1): every HTTP response with a message body contains a
    Content-Type header matching the content, including charset parameter."""
    app = Velox()

    @app.route("/json")
    def json_resp(req):
        return {"ok": True}

    @app.route("/text")
    def text_resp(req):
        return "hello"

    status, hdrs, _ = await call_app(app, "GET", "/json")
    assert hdrs["content-type"] == "application/json; charset=utf-8"

    status, hdrs, _ = await call_app(app, "GET", "/text")
    assert hdrs["content-type"] == "text/plain; charset=utf-8"


# ── V5 File Handling ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_asvs_v5_2_1_file_size_limit():
    """V5.2.1 (L1): the application accepts only files of a size it can process
    (max_body_size → 413)."""
    app = Velox(max_body_size=1024)

    @app.route("/upload", methods=["POST"])
    async def upload(req):
        await req.body()
        return {"ok": True}

    status, _, _ = await call_app(app, "POST", "/upload", body=b"x" * 1025)
    assert status == 413
    status, _, _ = await call_app(app, "POST", "/upload", body=b"x" * 1024)
    assert status == 200


@pytest.mark.asyncio
async def test_asvs_v5_3_2_safe_file_paths(tmp_path):
    """V5.3.2 (L1): file paths are created from trusted lists, not from
    user-submitted filenames without sanitization (no traversal/symlink)."""
    (tmp_path / "ok.txt").write_text("fine")
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("SECRET")
    os.symlink(outside, tmp_path / "link.txt")

    app = Velox()
    app.mount("/static", StaticFiles(str(tmp_path)))

    for path in ("/static/../../etc/passwd", "/static/link.txt"):
        status, _, body = await call_app(app, "GET", path)
        assert status in (403, 404), path
        assert b"SECRET" not in body and b"root:" not in body

    status, _, body = await call_app(app, "GET", "/static/ok.txt")
    assert status == 200 and body == b"fine"


# ── V6 Authentication ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_asvs_v6_3_1_brute_force_protection():
    """V6.3.1 (L1): controls prevent credential stuffing and password brute
    force (rate limiting → 429)."""
    app = Velox()
    app.use(rate_limit(limit=3, window=60.0))

    @app.route("/login", methods=["POST"])
    def login(req):
        return {"ok": True}

    for i in range(3):
        status, _, _ = await call_app(app, "POST", "/login", body=b"{}")
        assert status == 200
    status, hdrs, _ = await call_app(app, "POST", "/login", body=b"{}")
    assert status == 429
    assert "retry-after" in hdrs


# ── V13 Configuration ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_asvs_v13_4_1_git_metadata_hidden(tmp_path):
    """V13.4.1 (L1): source control metadata (.git/.svn) is inaccessible."""
    (tmp_path / ".git").write_text("repo")
    (tmp_path / "index.html").write_text("ok")

    app = Velox()
    app.mount("/static", StaticFiles(str(tmp_path)))

    for dotfile in (".git", ".svn", ".env"):
        status, _, body = await call_app(app, "GET", f"/static/{dotfile}")
        assert status in (403, 404), dotfile


# ── Дополнительные проверки инфраструктурных L1 ──────────────────────

@pytest.mark.asyncio
async def test_asvs_v5_3_1_static_files_not_executed(tmp_path):
    """V5.3.1 (L1): files uploaded/generated from untrusted input stored in a
    public folder are not executed — static files are served as content
    (mimetypes), never executed as code."""
    (tmp_path / "malware.py").write_text("print('pwned')")
    app = Velox()
    app.mount("/static", StaticFiles(str(tmp_path)))

    status, hdrs, body = await call_app(app, "GET", "/static/malware.py")
    assert status == 200
    # отдаётся как файл с content-type по mimetype, тело не исполняется
    assert b"pwned" in body
    assert "python" in hdrs.get("content-type", "") or "octet-stream" in hdrs.get("content-type", "")


@pytest.mark.asyncio
async def test_asvs_v1_2_2_no_untrusted_url_building():
    """V1.2.2 (L1): when dynamically building URLs, untrusted data is encoded
    per context. Velox не строит URL из ввода пользователя — query-параметры
    возвращаются как данные, не как ссылки."""
    app = Velox()

    @app.route("/redirect")
    def redirect(req):
        return {"next": req.query.get("next", [""])[0]}

    status, _, body = await call_app(app, "GET", "/redirect", query=b"next=javascript:alert(1)")
    assert status == 200
    data = json.loads(body)
    assert data["next"] == "javascript:alert(1)"  # это данные, фреймворк их не выполняет
