"""Rate limiting — in-memory sliding window middleware.

Usage:
    from ferronit.contrib.ratelimit import rate_limit
    app.use(rate_limit(limit=100, window=60.0))        # 100 req/min per IP
    app.use(rate_limit(limit=5, window=60.0, key=lambda req: req.get_header("x-api-key") or ""))
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from ferronit.core.response import Response

__all__ = ["rate_limit"]

_MAX_BUCKETS = 10_000


def _default_key(req) -> str:
    client = req._scope.get("client")
    return client[0] if client else "unknown"


def rate_limit(limit: int, window: float = 60.0, key=None):
    """Sliding-window rate limiter; returns 429 with Retry-After when exceeded."""
    _buckets: dict[str, deque[float]] = defaultdict(deque)
    get_key = key or _default_key

    async def rate_mw(req, next_handler):
        k = get_key(req)
        now = time.monotonic()
        times = _buckets[k]
        while times and times[0] <= now - window:
            times.popleft()

        if len(times) >= limit:
            retry_after = max(1.0, window - (now - times[0]))
            resp = Response(body=b"rate limit exceeded", status=429)
            resp._headers["Retry-After"] = f"{retry_after:.0f}"
            return resp

        times.append(now)

        # защита от неограниченного роста памяти
        if len(_buckets) > _MAX_BUCKETS:
            cutoff = now - window
            stale = [bk for bk, t in _buckets.items() if not t or t[-1] <= cutoff]
            for bk in stale:
                del _buckets[bk]

        result = next_handler(req)
        if hasattr(result, "__await__"):
            return await result
        return result

    return rate_mw
