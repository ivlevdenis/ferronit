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
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass
class ChatResponse:
    content: str
    model: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    raw: Any = None


class LlmPort(ABC):
    """Language model port — send prompts, get completions."""

    async def complete(self, prompt: str, **kwargs) -> str: ...

    async def chat(self, messages: list[ChatMessage], **kwargs) -> ChatResponse: ...

    async def embed(self, text: str | list[str], **kwargs) -> list[float]: ...


class MockLlmAdapter(LlmPort):
    """Mock adapter — returns predefined responses for testing."""

    def __init__(self, responses: dict[str, str] | None = None):
        self._responses = responses or {}
        self.history: list[dict] = []

    async def complete(self, prompt: str, **kwargs) -> str:
        self.history.append({"type": "complete", "prompt": prompt, "kwargs": kwargs})
        return self._responses.get("complete", f"Mock response to: {prompt[:50]}")

    async def chat(self, messages: list[ChatMessage], **kwargs) -> ChatResponse:
        self.history.append({"type": "chat", "messages": messages, "kwargs": kwargs})
        last = messages[-1].content if messages else ""
        return ChatResponse(
            content=self._responses.get("chat", f"Mock chat reply to: {last[:50]}"),
            model="mock/v1",
            usage={"prompt_tokens": 10, "completion_tokens": 5},
        )

    async def embed(self, text: str | list[str], **kwargs) -> list[float]:
        texts = [text] if isinstance(text, str) else text
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
        resp = await self.chat([ChatMessage(role="user", content=prompt)], **kwargs)
        return resp.content

    async def chat(self, messages: list[ChatMessage], **kwargs) -> ChatResponse:
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
        resp = await self.chat([ChatMessage(role="user", content=prompt)], **kwargs)
        return resp.content

    async def chat(self, messages: list[ChatMessage], **kwargs) -> ChatResponse:
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
        raise NotImplementedError("Claude does not provide embeddings API")
