"""Health checks — /health обязан отдавать 503, когда зависимость лежит.

Для readiness-пробы в k8s «200 при мёртвой базе» — фатальное поведение, поэтому
статус проверяется end-to-end через ASGI, а не только на уровне HealthCheck.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from ferrox import Ferrox
from ferrox.contrib.health import HealthCheck, HealthStatus


async def _ok() -> bool:
    return True


@pytest.fixture
def client_factory():
    def make(hc: HealthCheck):
        app = Ferrox(debug=True)
        app.route("/health")(hc)
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    return make


def test_health_status_fields():
    status = HealthStatus("db", False, "connection refused")
    assert (status.name, status.ok, status.detail) == ("db", False, "connection refused")


@pytest.mark.asyncio
async def test_check_all_supports_sync_and_async_checks():
    hc = HealthCheck()
    hc.add("sync_ok", lambda: True)
    hc.add("async_ok", _ok)
    hc.add("failing", lambda: False)

    results = await hc.check_all()

    assert [r.name for r in results] == ["sync_ok", "async_ok", "failing"]
    assert [r.ok for r in results] == [True, True, False]
    assert all(r.detail == "" for r in results)


@pytest.mark.asyncio
async def test_exception_becomes_failed_check_with_detail():
    def boom():
        raise RuntimeError("connection refused")

    hc = HealthCheck()
    hc.add("db", boom)

    (result,) = await hc.check_all()

    assert result.ok is False
    assert "connection refused" in result.detail
    assert hc.is_healthy is False


@pytest.mark.asyncio
async def test_is_healthy_is_true_without_checks_and_after_success():
    hc = HealthCheck()
    assert hc.is_healthy is True  # нечего проверять — нечего ломать

    hc.add("db", lambda: True)
    await hc.check_all()
    assert hc.is_healthy is True


@pytest.mark.asyncio
async def test_health_endpoint_200_when_dependencies_alive(client_factory):
    hc = HealthCheck()
    hc.add("db", lambda: True)

    async with client_factory(hc) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": [{"name": "db", "ok": True, "detail": ""}],
    }


@pytest.mark.asyncio
async def test_health_endpoint_503_when_dependency_down(client_factory):
    """Регрессия: кортеж (payload, status) раньше не понимался — /health всегда отдавал 200."""
    hc = HealthCheck()
    hc.add("db", lambda: False)

    async with client_factory(hc) as client:
        response = await client.get("/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"] == [{"name": "db", "ok": False, "detail": ""}]
