"""RAG pipeline without external services: vector port + local embedding adapter.

The key part of the example is the port implementation: `LocalTfVectorizer` inherits
`LlmPort` and fills in only `embed()`, so the search really ranks documents by
similarity instead of guessing. In production this spot holds `OpenAiAdapter`
(embeddings) — the interface is the same, the application lines stay unchanged:

    embedder: LlmPort = OpenAiAdapter()
    vectors: VectorDbPort = QdrantAdapter("http://localhost:6333")

Indexing is lazy: lifecycle hooks do not exist yet in 0.8 (a known limitation,
see AGENTS.md), so the corpus is loaded on the first request.

Run:
    .venv/bin/python -m granian --interface asgi --no-ws examples.rag_app:app
Check:
    curl -X POST localhost:8000/ask -H 'content-type: application/json' \
         -d '{"question": "why Rust in the core"}'
"""

from __future__ import annotations

import zlib

from ferronit import Ferronit
from ferronit.contrib.llm import ChatMessage, ChatResponse, LlmPort, MockLlmAdapter
from ferronit.contrib.vectordb import MockVectorDb, VectorDbPort

app = Ferronit()


class LocalTfVectorizer(LlmPort):
    """An LLM port where only ``embed()`` is real.

    A bag of words hashed into a fixed-length vector: no keys, no network, no
    models, yet cosine similarity still comes out meaningful. The string hash is
    salted per process — good enough for demos and tests.
    """

    def __init__(self, dim: int = 64) -> None:
        self._dim = dim

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self._dim
        for word in text.lower().replace(",", " ").replace(".", " ").split():
            # crc32, not hash(): the result does not depend on PYTHONHASHSEED,
            # otherwise ranking (and with it the example) drifts between runs
            vector[zlib.crc32(word.encode("utf-8")) % self._dim] += 1.0
        norm = sum(v * v for v in vector) ** 0.5 or 1.0
        return [v / norm for v in vector]

    async def embed(self, text: str | list[str], **kwargs) -> list[float]:
        """Dense vector for a string (for a list — the vector of the joined text)."""
        if isinstance(text, list):
            text = " ".join(text)
        return self._vector(text)

    async def complete(self, prompt: str, **kwargs) -> str:
        """Not used in this example — answers with an echo."""
        return f"(local) {prompt}"

    async def chat(self, messages: list[ChatMessage], **kwargs) -> ChatResponse:
        """Not used in this example: a separate adapter provides the answer."""
        last = messages[-1].content if messages else ""
        return ChatResponse(content=f"(local) {last}", model="local-tf")


embedder: LlmPort = LocalTfVectorizer()
llm: LlmPort = MockLlmAdapter(
    responses={"chat": "Rust отвечает за горячий путь, Python — за смысл."}
)
vectors: VectorDbPort = MockVectorDb()  # in production: QdrantAdapter("http://localhost:6333")

CORPUS: dict[str, str] = {
    "routing": "Роутинг и разбор пути живут в Rust, matchit: около 1% времени запроса.",
    "json": "JSON пишется msgspec C-энкодером и экранирует HTML-символы.",
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
    question = str(body.get("question") or "о чём Ferronit")
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
