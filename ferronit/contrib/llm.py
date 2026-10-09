"""LLM Port — language model abstraction for CQRS/DDD workflows."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ChatMessage",
    "ChatResponse",
    "ClaudeAdapter",
    "LlmPort",
    "MockLlmAdapter",
    "OpenAiAdapter",
]


@dataclass
class ChatMessage:
    """A single message in a chat conversation.

    Attributes:
        role: One of ``"system"``, ``"user"`` or ``"assistant"``.
        content: The message text.
    """

    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass
class ChatResponse:
    """A completion returned by a language model adapter.

    Attributes:
        content: The generated text.
        model: Model identifier reported by the provider.
        usage: Token usage counters; keys vary by provider.
        raw: Unmodified provider response payload, when available.
    """

    content: str
    model: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    raw: Any = None


class LlmPort(ABC):
    """Language model port — send prompts, get completions.

    The adapters (``OpenAiAdapter``, ``ClaudeAdapter``, ``MockLlmAdapter``) are
    interchangeable: application code depends only on this port, so a real
    provider can be swapped for the mock in tests without code changes.
    """

    @abstractmethod
    async def complete(self, prompt: str, **kwargs) -> str:
        """Generate text for a single prompt.

        Args:
            prompt: The user prompt.
            **kwargs: Provider-specific options (e.g. ``temperature``, ``model``).

        Returns:
            The generated text.
        """
        ...

    @abstractmethod
    async def chat(self, messages: list[ChatMessage], **kwargs) -> ChatResponse:
        """Generate a reply for a conversation.

        Args:
            messages: Ordered conversation history.
            **kwargs: Provider-specific options (e.g. ``temperature``, ``max_tokens``).

        Returns:
            A ``ChatResponse`` with the assistant reply and usage metadata.
        """
        ...

    @abstractmethod
    async def embed(self, text: str | list[str], **kwargs) -> list[float]:
        """Embed text into a vector.

        Args:
            text: A single string or a list of strings.
            **kwargs: Provider-specific options (e.g. ``model``).

        Returns:
            The embedding vector returned by the provider.
        """
        ...


class MockLlmAdapter(LlmPort):
    """Mock adapter — returns predefined responses for testing."""

    def __init__(self, responses: dict[str, str] | None = None):
        self._responses = responses or {}
        self.history: list[dict] = []

    async def complete(self, prompt: str, **kwargs) -> str:
        """Return a canned completion and record the call.

        Args:
            prompt: The user prompt, stored in ``history``.
            **kwargs: Recorded in ``history`` (not interpreted).

        Returns:
            ``responses["complete"]`` if configured, otherwise a default string.
        """
        self.history.append({"type": "complete", "prompt": prompt, "kwargs": kwargs})
        return self._responses.get("complete", f"Mock response to: {prompt[:50]}")

    async def chat(self, messages: list[ChatMessage], **kwargs) -> ChatResponse:
        """Return a canned chat reply and record the call.

        Args:
            messages: Conversation history, stored in ``history``.
            **kwargs: Recorded in ``history`` (not interpreted).

        Returns:
            A ``ChatResponse`` with ``model="mock/v1"`` and fixed token usage;
            the content comes from ``responses["chat"]`` when configured.
        """
        self.history.append({"type": "chat", "messages": messages, "kwargs": kwargs})
        last = messages[-1].content if messages else ""
        return ChatResponse(
            content=self._responses.get("chat", f"Mock chat reply to: {last[:50]}"),
            model="mock/v1",
            usage={"prompt_tokens": 10, "completion_tokens": 5},
        )

    async def embed(self, text: str | list[str], **kwargs) -> list[float]:
        """Return a fixed 128-dimension embedding vector.

        Args:
            text: Ignored by the mock.
            **kwargs: Ignored by the mock.

        Returns:
            A list of 128 ``0.1`` values.
        """
        return [0.1] * 128


# ── Real adapters ─────────────────────────────────────────────────────

class OpenAiAdapter(LlmPort):
    """OpenAI API adapter — async, no external deps beyond httpx."""

    def __init__(self, api_key: str = "", base_url: str = "https://api.openai.com/v1", model: str = "gpt-4o"):
        import os
        self._key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._base = base_url.rstrip("/")
        self._model = model

    async def complete(self, prompt: str, **kwargs) -> str:
        """Generate text for a single prompt via the chat completions API.

        Args:
            prompt: The user prompt, sent as a single ``user`` message.
            **kwargs: Forwarded to :meth:`chat`.

        Returns:
            The generated text.
        """
        resp = await self.chat([ChatMessage(role="user", content=prompt)], **kwargs)
        return resp.content

    async def chat(self, messages: list[ChatMessage], **kwargs) -> ChatResponse:
        """Call OpenAI's ``/chat/completions`` endpoint.

        Args:
            messages: Conversation history.
            **kwargs: Optional ``model`` override and ``temperature``.

        Returns:
            A ``ChatResponse`` with the reply text, model and token usage.

        Raises:
            httpx.HTTPStatusError: If the API returns a non-2xx response.
        """
        import httpx
        payload = {
            "model": kwargs.get("model", self._model),
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        if "temperature" in kwargs:
            payload["temperature"] = kwargs["temperature"]

        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                f"{self._base}/chat/completions",
                headers={"Authorization": f"Bearer {self._key}"},
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
            choice = data["choices"][0]["message"]
            return ChatResponse(
                content=choice["content"],
                model=data.get("model", self._model),
                usage=data.get("usage", {}),
                raw=data,
            )

    async def embed(self, text: str | list[str], **kwargs) -> list[float]:
        """Call OpenAI's ``/embeddings`` endpoint.

        Args:
            text: Input text; a single string is wrapped into a list.
            **kwargs: Optional ``model`` override (default ``text-embedding-3-small``).

        Returns:
            The embedding vector for the first input text.

        Raises:
            httpx.HTTPStatusError: If the API returns a non-2xx response.
        """
        import httpx
        texts = [text] if isinstance(text, str) else text
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{self._base}/embeddings",
                headers={"Authorization": f"Bearer {self._key}"},
                json={"model": kwargs.get("model", "text-embedding-3-small"), "input": texts},
            )
            r.raise_for_status()
            data = r.json()
            return data["data"][0]["embedding"]


class ClaudeAdapter(LlmPort):
    """Anthropic Claude API adapter."""

    def __init__(self, api_key: str = "", model: str = "claude-sonnet-4-20250514"):
        import os
        self._key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._model = model

    async def complete(self, prompt: str, **kwargs) -> str:
        """Generate text for a single prompt via the messages API.

        Args:
            prompt: The user prompt, sent as a single ``user`` message.
            **kwargs: Forwarded to :meth:`chat`.

        Returns:
            The generated text.
        """
        resp = await self.chat([ChatMessage(role="user", content=prompt)], **kwargs)
        return resp.content

    async def chat(self, messages: list[ChatMessage], **kwargs) -> ChatResponse:
        """Call Anthropic's ``/v1/messages`` endpoint.

        System messages are pulled out into the top-level ``system`` field as
        required by the Claude API.

        Args:
            messages: Conversation history.
            **kwargs: Optional ``model`` override and ``max_tokens`` (default 1024).

        Returns:
            A ``ChatResponse`` with the reply text, model and token usage.

        Raises:
            httpx.HTTPStatusError: If the API returns a non-2xx response.
        """
        import httpx
        system = ""
        msgs = []
        for m in messages:
            if m.role == "system":
                system = m.content
            else:
                msgs.append({"role": m.role, "content": m.content})

        payload = {
            "model": kwargs.get("model", self._model),
            "max_tokens": kwargs.get("max_tokens", 1024),
            "messages": msgs,
        }
        if system:
            payload["system"] = system

        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self._key,
                    "anthropic-version": "2023-06-01",
                },
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
            return ChatResponse(
                content=data["content"][0]["text"],
                model=data.get("model", self._model),
                usage={k: data["usage"].get(k, 0) for k in ("input_tokens", "output_tokens")},
                raw=data,
            )

    async def embed(self, text: str | list[str], **kwargs) -> list[float]:
        """Unsupported — Claude has no embeddings API.

        Args:
            text: Ignored.
            **kwargs: Ignored.

        Raises:
            NotImplementedError: Always; use ``OpenAiAdapter`` for embeddings.
        """
        raise NotImplementedError("Claude does not provide embeddings API")
