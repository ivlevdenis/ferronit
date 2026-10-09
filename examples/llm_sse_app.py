"""LLM как порт: обычный ответ и SSE-стриминг токенов.

Смена вендора — одна строка композиции:
    llm: LlmPort = OpenAiAdapter()      # или ClaudeAdapter()
    llm: LlmPort = MockLlmAdapter(...)  # тесты и демо без ключей и сети

Запуск:
    .venv/bin/python -m granian --interface asgi --no-ws examples.llm_sse_app:app
Проверка:
    curl -X POST localhost:8000/chat -H 'content-type: application/json' \
         -d '{"prompt": "что такое Ferronit"}'
    curl -N -X POST localhost:8000/chat/stream -H 'content-type: application/json' \
         -d '{"prompt": "стриминг"}'
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from ferronit import StreamingResponse, Ferronit
from ferronit.contrib.llm import ChatMessage, LlmPort, MockLlmAdapter

app = Ferronit()

# Порт. В проде здесь OpenAiAdapter()/ClaudeAdapter() — код ниже не меняется.
llm: LlmPort = MockLlmAdapter(
    responses={"chat": "Ferronit отдаёт ответ модели по мере генерации, без буферизации."}
)


def _prompt_of(body: dict) -> str:
    """Read the prompt from a request body with a safe default."""
    return str(body.get("prompt") or "привет")


@app.route("/chat", methods=["POST"])
async def chat(req) -> dict:
    """Ask the model and return the whole answer at once."""
    body = await req.json()
    reply = await llm.chat([ChatMessage("user", _prompt_of(body))])
    return {"reply": reply.content, "model": reply.model, "usage": reply.usage}


async def _token_stream(text: str) -> AsyncIterator[str]:
    """Yield the answer word by word in SSE format."""
    for word in text.split():
        payload = json.dumps({"token": word + " "}, ensure_ascii=False)
        yield f"data: {payload}\n\n"
        await asyncio.sleep(0.01)  # имитация скорости генерации
    yield "data: [DONE]\n\n"


@app.route("/chat/stream", methods=["POST"])
async def chat_stream(req) -> StreamingResponse:
    """Same answer, streamed as Server-Sent Events for a chat UI."""
    body = await req.json()
    reply = await llm.chat([ChatMessage("user", _prompt_of(body))])
    return StreamingResponse(_token_stream(reply.content))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, port=8000)
