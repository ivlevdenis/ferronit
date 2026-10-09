"""Порт векторной БД и Mock-адаптер — поведение проверяется без внешних сервисов."""

import pytest

from ferronit.contrib.vectordb import (
    ChromaAdapter,
    MockVectorDb,
    QdrantAdapter,
    VectorDbPort,
    _cosine,
)


def test_port_cannot_be_instantiated():
    with pytest.raises(TypeError):
        VectorDbPort()  # type: ignore[abstract]


@pytest.mark.asyncio
async def test_search_returns_best_match_first_with_metadata():
    db = MockVectorDb()
    await db.upsert(
        "docs",
        ["a", "b"],
        [[1.0, 0.0], [0.0, 1.0]],
        [{"title": "A"}, {"title": "B"}],
    )

    hits = await db.search("docs", [1.0, 0.0], limit=2)

    assert [h["id"] for h in hits] == ["a", "b"]
    assert hits[0]["score"] == pytest.approx(1.0)
    assert hits[0]["metadata"] == {"title": "A"}


@pytest.mark.asyncio
async def test_upsert_without_metadata_and_limit_is_respected():
    db = MockVectorDb()
    await db.upsert("docs", ["a", "b", "c"], [[1.0, 0.0], [0.9, 0.1], [0.0, 1.0]])

    hits = await db.search("docs", [1.0, 0.0], limit=2)

    assert len(hits) == 2
    assert hits[0]["metadata"] == {}


@pytest.mark.asyncio
async def test_search_in_unknown_collection_returns_empty():
    assert await MockVectorDb().search("missing", [1.0, 0.0]) == []


@pytest.mark.asyncio
async def test_delete_removes_vectors_and_is_idempotent():
    db = MockVectorDb()
    await db.upsert("docs", ["a"], [[1.0, 0.0]])

    await db.delete("docs", ["a"])
    assert await db.search("docs", [1.0, 0.0]) == []

    await db.delete("docs", ["a"])  # повторное удаление не должно падать


def test_cosine_similarity_edges():
    assert _cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert _cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert _cosine([0.0, 0.0], [1.0, 0.0]) == 0.0  # нулевой вектор — без ZeroDivisionError


def test_remote_adapters_construct_without_running_services():
    """Конструкторы адаптеров не ходят в сеть — соединение только в вызовах методов."""
    assert QdrantAdapter("http://localhost:6333")._url == "http://localhost:6333"
    assert ChromaAdapter("http://localhost:8000")._url == "http://localhost:8000"
