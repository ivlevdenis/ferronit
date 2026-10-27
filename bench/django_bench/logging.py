"""Access-log middleware for the Django benchmark (one line per request).

Enabled only when ``BENCH_ACCESS_LOG=1`` (the benchmark settings add this module
to ``MIDDLEWARE`` conditionally). Logs ``method path status duration_ms`` to
stdout via the standard library, matching the other frameworks' access log.
"""

import logging
import time

logger = logging.getLogger("bench.access")
logger.setLevel(logging.INFO)
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(_handler)
logger.propagate = False


class AccessLogMiddleware:
    """Log ``method path status duration_ms`` for every request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        start = time.perf_counter()
        response = self.get_response(request)
        logger.info(
            "%s %s %d %.3fms",
            request.method,
            request.path,
            response.status_code,
            (time.perf_counter() - start) * 1000,
        )
        return response
