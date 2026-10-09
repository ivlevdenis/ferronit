"""Security headers — production defaults in one middleware.

Usage:
    from ferronit.contrib.security import security_headers
    app.use(security_headers())                 # all defaults
    app.use(security_headers(hsts=False, csp="default-src 'self'"))
"""

from __future__ import annotations

from ferronit.core.response import Response

__all__ = ["security_headers"]

HSTS_DEFAULT = "max-age=31536000; includeSubDomains"
REFERRER_DEFAULT = "no-referrer"


def security_headers(
    *,
    nosniff: bool = True,
    frame: str = "DENY",
    referrer: str | None = REFERRER_DEFAULT,
    hsts: str | None = HSTS_DEFAULT,
    csp: str | None = None,
):
    """Add production security headers to every response.

    - X-Content-Type-Options: nosniff        (MIME-sniffing)
    - X-Frame-Options: DENY                  (clickjacking)
    - Referrer-Policy: no-referrer           (referrer leak)
    - Strict-Transport-Security (HSTS)       (TLS downgrade; off for HTTP dev)
    - Content-Security-Policy                (optional, opt-in)
    """
    headers: dict[str, str] = {}
    if nosniff:
        headers["X-Content-Type-Options"] = "nosniff"
    if frame:
        headers["X-Frame-Options"] = frame
    if referrer:
        headers["Referrer-Policy"] = referrer
    if hsts:
        headers["Strict-Transport-Security"] = hsts
    if csp:
        headers["Content-Security-Policy"] = csp

    async def security_mw(req, next_handler):
        result = next_handler(req)
        if hasattr(result, "__await__"):
            result = await result
        if not isinstance(result, Response):
            # dict/str → сериализуем сейчас, чтобы заголовки попали в ответ
            from ferronit.core.app import _to_response
            result = _to_response(result)
        for k, v in headers.items():
            result._headers.setdefault(k, v)
        return result

    return security_mw
