"""Config + DI + Tracing + LLM tests."""

import pytest
from httpx import ASGITransport, AsyncClient

from ferronit import Ferronit
from ferronit.config import EnvConfig
from ferronit.contrib.llm import ChatMessage, MockLlmAdapter
from ferronit.contrib.tracing import trace_middleware
from ferronit.di import Container
from ferronit.hexagonal import Logger, PrintLogger

# ── Config ────────────────────────────────────────────────────────────

def test_env_config_typed():
    c = EnvConfig(env={"V_PORT": "9090", "V_DEBUG": "1", "V_HOSTS": "a, b, c"})
    assert c.int("V_PORT") == 9090
    assert c.bool("V_DEBUG") is True
    assert c.list("V_HOSTS") == ["a", "b", "c"]
    assert c.get("V_MISSING", "x") == "x"


def test_env_config_prefix():
    c = EnvConfig(prefix="APP_", env={"APP_DB": "pg://", "APP_PORT": "3000"})
    assert c.get("DB") == "pg://"
    assert c.int("PORT") == 3000


def test_env_config_defaults():
    c = EnvConfig(env={})
    assert c.int("PORT", 8000) == 8000
    assert c.bool("DEBUG") is False
    assert c.list("EMPTY") == []


# ── DI Container ──────────────────────────────────────────────────────

def test_di_singleton_and_factory():
    c = Container()
    c.singleton(Logger, PrintLogger())
    c.factory(Logger)

    logger1 = c.get(Logger)
    logger2 = c.get(Logger)
    assert logger1 is logger2


def test_di_resolve_with_defaults():
    class WithDefault:
        def __init__(self, logger: Logger, name: str = "default"):
            self.name = name

    c = Container()
    c.singleton(Logger, PrintLogger())
    c.factory(WithDefault)
    inst = c.get(WithDefault)
    assert inst.name == "default"


def test_di_missing_raises():
    c = Container()
    c.factory(PrintLogger)
    with pytest.raises(KeyError, match="Logger"):
        c.get(Logger)


# ── LLM Mock ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mock_llm_chat():
    llm = MockLlmAdapter({"chat": "Hello!"})
    resp = await llm.chat([ChatMessage("user", "Hi")])
    assert resp.content == "Hello!"
    assert resp.model == "mock/v1"
    assert len(llm.history) == 1


@pytest.mark.asyncio
async def test_mock_llm_embed():
    llm = MockLlmAdapter()
    emb = await llm.embed("test")
    assert len(emb) == 128


# ── Tracing ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_trace_id_generated():
    v = Ferronit()
    v.use(trace_middleware)

    @v.route("/")
    def home(req):
        return {"ok": True}

    transport = ASGITransport(app=v)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/")
        tid = r.headers.get("x-trace-id")
        assert tid is not None
        assert "-" in tid  # UUID format


@pytest.mark.asyncio
async def test_trace_id_propagated():
    v = Ferronit()
    v.use(trace_middleware)

    @v.route("/")
    def home(req):
        return {"ok": True}

    transport = ASGITransport(app=v)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/", headers={"X-Trace-Id": "my-custom-id"})
        assert r.headers.get("x-trace-id") == "my-custom-id"
