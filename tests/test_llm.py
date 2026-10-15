"""LLM-адаптеры: сборка запроса, разбор ответа и ошибки — на подменённом httpx.

Живой провайдер не нужен и не должен вызываться: подменяем `httpx.AsyncClient`
заглушкой, которая запоминает запрос и отдаёт заранее заданный ответ. Так
проверяется ровно наш код (payload, заголовки, разбор), а не сеть.
"""

from __future__ import annotations

import httpx
import pytest

from ferronit.contrib.llm import (
    ChatMessage,
    ChatResponse,
    ClaudeAdapter,
    LlmPort,
    MockLlmAdapter,
    OpenAiAdapter,
)


class _FakeResponse:
    """Ответ провайдера: payload + статус (для проверки ошибок)."""

    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", "http://provider.test")
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=request,
                response=httpx.Response(self.status_code, request=request),
            )


class _FakeClient:
    """Заглушка `httpx.AsyncClient`: помнит запросы, отдаёт ответы по порядку."""

    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def post(self, url: str, headers: dict | None = None, json: dict | None = None):
        self.calls.append({"url": url, "headers": headers or {}, "json": json})
        return self._responses.pop(0)


@pytest.fixture
def fake_httpx(monkeypatch: pytest.MonkeyPatch):
    """Установить заглушку httpx.AsyncClient и вернуть её установщик."""

    def install(*responses: _FakeResponse) -> _FakeClient:
        client = _FakeClient(list(responses))
        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: client)
        return client

    return install


def _openai_payload(content: str = "привет", model: str = "gpt-4o") -> dict:
    return {
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3},
    }


# ── OpenAI ─────────────────────────────────────────────────────────────


async def test_openai_chat_sends_messages_and_parses_answer(fake_httpx) -> None:
    """Chat: тело запроса, заголовок с ключом, разбор ответа в ChatResponse."""
    client = fake_httpx(_FakeResponse(_openai_payload("привет")))
    adapter = OpenAiAdapter(api_key="sk-test", model="gpt-4o")

    result = await adapter.chat([ChatMessage(role="user", content="как дела?")])

    assert isinstance(result, ChatResponse)
    assert result.content == "привет"
    assert result.model == "gpt-4o"
    assert result.usage == {"prompt_tokens": 7, "completion_tokens": 3}
    assert result.raw["model"] == "gpt-4o"

    call = client.calls[0]
    assert call["url"] == "https://api.openai.com/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer sk-test"
    assert call["json"] == {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "как дела?"}],
    }


async def test_openai_chat_passes_model_and_temperature_overrides(fake_httpx) -> None:
    """Переопределение модели и temperature уходит в тело запроса."""
    client = fake_httpx(_FakeResponse(_openai_payload()))
    adapter = OpenAiAdapter(api_key="sk-test")

    await adapter.chat([ChatMessage(role="user", content="x")], model="gpt-4o-mini", temperature=0.2)

    assert client.calls[0]["json"]["model"] == "gpt-4o-mini"
    assert client.calls[0]["json"]["temperature"] == 0.2


async def test_openai_complete_wraps_prompt_into_user_message(fake_httpx) -> None:
    """Complete — тот же chat, но промпт становится одним user-сообщением."""
    client = fake_httpx(_FakeResponse(_openai_payload("ответ")))

    assert await OpenAiAdapter(api_key="k").complete("вопрос") == "ответ"
    assert client.calls[0]["json"]["messages"] == [{"role": "user", "content": "вопрос"}]


async def test_openai_embed_handles_string_and_list(fake_httpx) -> None:
    """Embed: строка заворачивается в список, возвращается первый вектор."""
    payload = {"data": [{"embedding": [0.1, 0.2, 0.3]}]}
    client = fake_httpx(_FakeResponse(payload), _FakeResponse(payload))
    adapter = OpenAiAdapter(api_key="k")

    assert await adapter.embed("текст") == [0.1, 0.2, 0.3]
    assert client.calls[0]["json"] == {"model": "text-embedding-3-small", "input": ["текст"]}

    await adapter.embed(["a", "b"], model="text-embedding-3-large")
    assert client.calls[1]["json"] == {"model": "text-embedding-3-large", "input": ["a", "b"]}


async def test_openai_raises_on_error_status(fake_httpx) -> None:
    """Ошибка провайдера не глотается — уходит наружу."""
    fake_httpx(_FakeResponse({}, status=500))

    with pytest.raises(httpx.HTTPStatusError):
        await OpenAiAdapter(api_key="k").chat([ChatMessage(role="user", content="x")])


async def test_openai_takes_key_from_environment(fake_httpx, monkeypatch) -> None:
    """Ключ без явного аргумента берётся из окружения."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    client = fake_httpx(_FakeResponse(_openai_payload()))

    await OpenAiAdapter().complete("x")

    assert client.calls[0]["headers"]["Authorization"] == "Bearer sk-env"


# ── Claude ─────────────────────────────────────────────────────────────


async def test_claude_moves_system_message_to_top_level(fake_httpx) -> None:
    """System-сообщение уходит в отдельное поле, остальные — в messages."""
    payload = {
        "model": "claude-sonnet-4-20250514",
        "content": [{"type": "text", "text": "готово"}],
        "usage": {"input_tokens": 11, "output_tokens": 5},
    }
    client = fake_httpx(_FakeResponse(payload))
    adapter = ClaudeAdapter(api_key="anthropic-key")

    result = await adapter.chat([
        ChatMessage(role="system", content="ты строгий"),
        ChatMessage(role="user", content="привет"),
    ])

    assert result.content == "готово"
    assert result.usage == {"input_tokens": 11, "output_tokens": 5}
    call = client.calls[0]
    assert call["url"] == "https://api.anthropic.com/v1/messages"
    assert call["headers"] == {"x-api-key": "anthropic-key", "anthropic-version": "2023-06-01"}
    assert call["json"]["system"] == "ты строгий"
    assert call["json"]["messages"] == [{"role": "user", "content": "привет"}]
    assert call["json"]["max_tokens"] == 1024


async def test_claude_max_tokens_override_and_env_key(fake_httpx, monkeypatch) -> None:
    """max_tokens переопределяется, ключ может прийти из окружения."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-env")
    client = fake_httpx(_FakeResponse(
        {"content": [{"text": "x"}], "usage": {"input_tokens": 1, "output_tokens": 1}}
    ))

    await ClaudeAdapter().chat([ChatMessage(role="user", content="x")], max_tokens=256)

    assert client.calls[0]["json"]["max_tokens"] == 256
    assert client.calls[0]["headers"]["x-api-key"] == "anthropic-env"
    assert "system" not in client.calls[0]["json"]  # системного сообщения не было


async def test_claude_has_no_embeddings() -> None:
    """У Claude нет эмбеддингов — явная ошибка вместо пустого вектора."""
    with pytest.raises(NotImplementedError):
        await ClaudeAdapter(api_key="k").embed("текст")


# ── Mock и порт ────────────────────────────────────────────────────────


async def test_mock_adapter_returns_canned_answers_and_records_history() -> None:
    """Мок отдаёт заданные ответы и ведёт историю вызовов."""
    adapter = MockLlmAdapter({"complete": "готово", "chat": "ответ"})

    assert await adapter.complete("вопрос", temperature=0.5) == "готово"
    reply = await adapter.chat([ChatMessage(role="user", content="привет")])
    assert reply.content == "ответ"
    assert reply.model == "mock/v1"
    assert reply.usage == {"prompt_tokens": 10, "completion_tokens": 5}
    assert await adapter.embed("текст") == [0.1] * 128

    assert adapter.history[0] == {
        "type": "complete",
        "prompt": "вопрос",
        "kwargs": {"temperature": 0.5},
    }
    assert adapter.history[1]["type"] == "chat"
    assert adapter.history[1]["kwargs"] == {}


async def test_mock_adapter_without_responses_uses_defaults() -> None:
    """Без настройки мок отвечает по умолчанию и не падает на пустой истории."""
    adapter = MockLlmAdapter()

    assert (await adapter.complete("длинный вопрос " * 10)).startswith("Mock response to:")
    assert await adapter.chat([]) == ChatResponse(
        content="Mock chat reply to: ", model="mock/v1",
        usage={"prompt_tokens": 10, "completion_tokens": 5},
    )


def test_llm_port_is_abstract() -> None:
    """Порт нельзя инстанциировать — адаптеры обязаны реализовать три метода."""
    with pytest.raises(TypeError):
        LlmPort()  # type: ignore[abstract]
