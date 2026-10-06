"""Health checks — dependency-aware /health endpoint."""

from __future__ import annotations

from typing import Any

__all__ = ["HealthCheck", "HealthStatus"]


class HealthStatus:
    """Result of a single health check.

    Attributes:
        name: Name the check was registered under.
        ok: Whether the check passed.
        detail: Optional human-readable failure detail.
    """

    __slots__ = ("detail", "name", "ok")
    def __init__(self, name: str, ok: bool, detail: str = ""):
        self.name = name
        self.ok = ok
        self.detail = detail


class HealthCheck:
    """Collect health status from registered checkers.

    The instance itself is the ASGI handler: it returns ``(payload, status_code)``
    with 200 when every check passes and 503 otherwise.

    Usage:
        hc = HealthCheck()
        hc.add("db", check_db)
        hc.add("kafka", check_kafka)
        app.route("/health")(hc)          # 200 / 503 в зависимости от проверок
        if not hc.is_healthy:  # результат последнего check_all()
            ...
    """

    __slots__ = ("_checks", "_last")

    def __init__(self):
        self._checks: list[tuple[str, Any]] = []
        self._last: list[HealthStatus] = []

    def add(self, name: str, check):
        """Register a check: sync or async callable returning bool or HealthStatus."""
        self._checks.append((name, check))

    async def check_all(self) -> list[HealthStatus]:
        """Run every registered check and collect their statuses.

        Sync and async checks are both supported. A check returning a
        :class:`HealthStatus` is used as-is; any other value is coerced with
        ``bool``. Exceptions are captured as a failed status whose ``detail``
        holds the error message. The run is recorded as the last result
        backing :attr:`is_healthy`.

        Returns:
            One :class:`HealthStatus` per registered check.
        """
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
        self._last = results
        return results

    @property
    def is_healthy(self) -> bool:
        """``True`` if every check passed in the last ``check_all()`` run."""
        return all(r.ok for r in self._last)

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
