"""RAG-пайплайн без внешних сервисов: векторный порт + локальный адаптер эмбеддингов.

Важная часть примера — реализация порта: `LocalTfVectorizer` наследует `LlmPort` и
заполняет только `embed()`, поэтому поиск действительно ранжирует документы по
похожести, а не «угадывает». В проде на этом месте `OpenAiAdapter` (embeddings) —
интерфейс тот же, строки приложения не меняются:

    embedder: LlmPort = OpenAiAdapter()
    vectors: VectorDbPort = QdrantAdapter("http://localhost:6333")

Наполнение индекса ленивое: хуков жизненного цикла в 0.8 ещё нет (известное
ограничение, см. AGENTS.md), поэтому корпус загружается при первом запросе.

Запуск:
    .venv/bin/python -m granian --interface asgi --no-ws examples.rag_app:app
Проверка:
    curl -X POST localhost:8000/ask -H 'content-type: application/json' \
         -d '{"question": "зачем Rust в ядре"}'
"""

from __future__ import annotations

import zlib

from ferrox import Ferrox
from ferrox.contrib.llm import ChatMessage, ChatResponse, LlmPort, MockLlmAdapter
from ferrox.contrib.vectordb import MockVectorDb, VectorDbPort

app = Ferrox()


class LocalTfVectorizer(LlmPort):
    """Порт LLM, у которого настоящий только ``embed()``.

    Мешок слов, хешированный в вектор фиксированной длины: без ключей, сети и
    моделей, но косинусная близость получается осмысленной. Хеш строки солится
    в пределах процесса — для демо и тестов этого достаточно.
    """

    def __init__(self, dim: int = 64) -> None:
        self._dim = dim

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self._dim
        for word in text.lower().replace(",", " ").replace(".", " ").split():
            # crc32, а не hash(): результат не зависит от PYTHONHASHSEED,
            # иначе ранжирование (а с ним и пример) плавает между запусками
            vector[zlib.crc32(word.encode("utf-8")) % self._dim] += 1.0
        norm = sum(v * v for v in vector) ** 0.5 or 1.0
        return [v / norm for v in vector]

    async def embed(self, text: str | list[str], **kwargs) -> list[float]:
        """Плотный вектор для строки (для списка — вектор объединённого текста)."""
        if isinstance(text, list):
            text = " ".join(text)
        return self._vector(text)

    async def complete(self, prompt: str, **kwargs) -> str:
        """Не используется в этом примере — отвечает эхом."""
        return f"(local) {prompt}"

    async def chat(self, messages: list[ChatMessage], **kwargs) -> ChatResponse:
        """Не используется в этом примере: ответ даёт отдельный адаптер."""
        last = messages[-1].content if messages else ""
        return ChatResponse(content=f"(local) {last}", model="local-tf")


embedder: LlmPort = LocalTfVectorizer()
llm: LlmPort = MockLlmAdapter(responses={"chat": "Rust отвечает за горячий путь, Python — за смысл."})
vectors: VectorDbPort = MockVectorDb()  # в проде: QdrantAdapter("http://localhost:6333")

CORPUS: dict[str, str] = {
    "routing": "Роутинг и разбор пути живут в Rust, matchit: около 1% времени запроса.",
    "json": "JSON пишется serde_json одним проходом и экранирует HTML-символы.",
    "ddd": "DDD, CQRS и hexagonal дают каркас: агрегат, CommandBus, порты и адаптеры.",
}

_seeded = False


async def _seed_once() -> None:
    """Index the corpus on the first request (lazy seeding)."""
    global _seeded
    if _seeded:
        return
    ids = list(CORPUS)
    vectors_list = [await embedder.embed(CORPUS[i]) for i in ids]
    await vectors.upsert("docs", ids, vectors_list, [{"text": CORPUS[i]} for i in ids])
    _seeded = True


@app.route("/ask", methods=["POST"])
async def ask(req) -> dict:
    """Answer a question using the top-1 retrieved document as context."""
    body = await req.json()
    question = str(body.get("question") or "о чём Ferrox")
    await _seed_once()

    query_vector = await embedder.embed(question)
    hits = await vectors.search("docs", query_vector, limit=2)
    context = hits[0]["metadata"]["text"] if hits else "контекст не найден"

    reply = await llm.chat(
        [ChatMessage("system", f"Контекст: {context}"), ChatMessage("user", question)]
    )
    return {
        "question": question,
        "answer": reply.content,
        "sources": [{"id": h["id"], "score": round(h["score"], 3)} for h in hits],
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, port=8000)
