"""Vector Database port + adapters — Qdrant, Chroma, Mock."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

__all__ = ["VectorDbPort", "QdrantAdapter", "ChromaAdapter", "MockVectorDb"]


class VectorDbPort(ABC):
    """Semantic search port — upsert, search, delete embeddings."""

    async def upsert(self, collection: str, ids: list[str], vectors: list[list[float]],
                     metadata: list[dict] | None = None) -> None: ...

    async def search(self, collection: str, vector: list[float],
                     limit: int = 10) -> list[dict]: ...

    async def delete(self, collection: str, ids: list[str]) -> None: ...


# ── Qdrant ────────────────────────────────────────────────────────────

class QdrantAdapter(VectorDbPort):
    """Qdrant vector database adapter."""

    def __init__(self, url: str = "http://localhost:6333", api_key: str = ""):
        self._url = url.rstrip("/")
        self._key = api_key

    async def upsert(self, collection, ids, vectors, metadata=None):
        import httpx
        points = [
            {
                "id": ids[i],
                "vector": vectors[i],
                "payload": metadata[i] if metadata else {},
            }
            for i in range(len(ids))
        ]
        async with httpx.AsyncClient() as c:
            r = await c.put(
                f"{self._url}/collections/{collection}/points",
                headers={"api-key": self._key} if self._key else {},
                json={"points": points},
            )
            r.raise_for_status()

    async def search(self, collection, vector, limit=10):
        import httpx
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{self._url}/collections/{collection}/points/search",
                headers={"api-key": self._key} if self._key else {},
                json={"vector": vector, "limit": limit, "with_payload": True},
            )
            r.raise_for_status()
            data = r.json()
            return [
                {"id": p["id"], "score": p["score"], "metadata": p.get("payload", {})}
                for p in data.get("result", [])
            ]

    async def delete(self, collection, ids):
        import httpx
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{self._url}/collections/{collection}/points/delete",
                headers={"api-key": self._key} if self._key else {},
                json={"points": ids},
            )
            r.raise_for_status()


# ── Chroma ────────────────────────────────────────────────────────────

class ChromaAdapter(VectorDbPort):
    """Chroma vector database adapter (open-source, local-first)."""

    def __init__(self, url: str = "http://localhost:8000"):
        self._url = url.rstrip("/")

    async def upsert(self, collection, ids, vectors, metadata=None):
        import httpx
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{self._url}/collections/{collection}/upsert",
                json={
                    "ids": ids,
                    "embeddings": vectors,
                    "metadatas": metadata or [{}] * len(ids),
                },
            )
            r.raise_for_status()

    async def search(self, collection, vector, limit=10):
        import httpx
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{self._url}/collections/{collection}/query",
                json={"query_embeddings": [vector], "n_results": limit},
            )
            r.raise_for_status()
            data = r.json()
            results = []
            if data.get("ids") and data["ids"][0]:
                for i, id_ in enumerate(data["ids"][0]):
                    results.append({
                        "id": id_,
                        "score": data.get("distances", [[0]])[0][i] if data.get("distances") else 0,
                        "metadata": data.get("metadatas", [[{}]])[0][i] if data.get("metadatas") else {},
                    })
            return results

    async def delete(self, collection, ids):
        import httpx
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{self._url}/collections/{collection}/delete",
                json={"ids": ids},
            )
            r.raise_for_status()


# ── Mock ──────────────────────────────────────────────────────────────

class MockVectorDb(VectorDbPort):
    """In-memory vector DB for testing — brute-force cosine similarity."""

    def __init__(self):
        self._store: dict[str, dict[str, tuple[list[float], dict]]] = {}

    async def upsert(self, collection, ids, vectors, metadata=None):
        col = self._store.setdefault(collection, {})
        for i, id_ in enumerate(ids):
            col[id_] = (vectors[i], metadata[i] if metadata else {})

    async def search(self, collection, vector, limit=10):
        col = self._store.get(collection, {})
        scored = []
        for id_, (vec, meta) in col.items():
            score = _cosine(vector, vec)
            scored.append({"id": id_, "score": score, "metadata": meta})
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:limit]

    async def delete(self, collection, ids):
        col = self._store.get(collection, {})
        for id_ in ids:
            col.pop(id_, None)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
