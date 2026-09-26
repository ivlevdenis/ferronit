"""Health checks — dependency-aware /health endpoint."""

from __future__ import annotations

from typing import Any

from velox.hexagonal import Port

__all__ = ["HealthCheck", "HealthStatus"]


class HealthStatus:
    __slots__ = ("name", "ok", "detail")
    def __init__(self, name: str, ok: bool, detail: str = ""):
        self.name = name
        self.ok = ok
        self.detail = detail


class HealthCheck:
    """Collect health status from registered checkers.

    Usage:
        hc = HealthCheck()
        hc.add("db", check_db)
        hc.add("kafka", check_kafka)
        app.route("/health")(hc.endpoint)
    """

    __slots__ = ("_checks",)

    def __init__(self):
        self._checks: list[tuple[str, Any]] = []

    def add(self, name: str, check):
        """Register a check: sync or async callable returning bool or HealthStatus."""
        self._checks.append((name, check))

    async def check_all(self) -> list[HealthStatus]:
        results = []
        for name, check in self._checks:
            try:
                result = check()
                if hasattr(result, "__await__"):
                    result = await result
                if isinstance(result, HealthStatus):
                    results.append(result)
                else:
                    results.append(HealthStatus(name, bool(result)))
            except Exception as e:
                results.append(HealthStatus(name, False, str(e)))
        return results

    @property
    def is_healthy(self) -> bool:
        return all(r.ok for r in self._results if hasattr(self, "_results"))

    async def __call__(self, req):
        results = await self.check_all()
        healthy = all(r.ok for r in results)
        status_code = 200 if healthy else 503
        return {
            "status": "ok" if healthy else "degraded",
            "checks": [
                {"name": r.name, "ok": r.ok, "detail": r.detail} for r in results
            ],
        }, status_code
