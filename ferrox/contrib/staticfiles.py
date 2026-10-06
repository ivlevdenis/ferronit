"""Static files serving — directory mount with caching and range support."""

from __future__ import annotations

import mimetypes
import os
from wsgiref.handlers import format_date_time

from ferrox.core.app import Ferrox
from ferrox.core.request import Request
from ferrox.core.response import Response

__all__ = ["StaticFiles"]

_STATIC_CACHE: dict[str, tuple[bytes, str, int, float]] = {}


class StaticFiles:
    """Serve static files from a directory."""

    __slots__ = ("_cache_ttl", "_dir", "_prefix")

    def __init__(self, directory: str, cache_ttl: int = 3600) -> None:
        self._dir = os.path.abspath(directory)
        self._prefix = ""
        self._cache_ttl = cache_ttl

    def mount(self, app: Ferrox, prefix: str = "/static") -> None:
        """Register a catch-all route serving files under a URL prefix.

        Files are resolved relative to the directory given at construction;
        requests never escape that directory (symlinks are resolved and hidden
        files are not served).

        Args:
            app: Application to attach the static route to.
            prefix: URL prefix the files are served under.
        """
        self._prefix = prefix
        self_dir = self._dir
        cache_ttl = self._cache_ttl

        # Register catch-all route with prefix
        @app.route(f"{prefix}/{{path:path}}")
        async def serve_static(req: Request):
            file_path = req.path[len(prefix) + 1:]  # strip prefix
            return _serve_file(self_dir, file_path, req, cache_ttl)


def _serve_file(base_dir: str, file_path: str, req: Request, cache_ttl: int) -> Response:
    safe_path = os.path.normpath(file_path).lstrip("/")
    full_path = os.path.realpath(os.path.join(base_dir, safe_path))  # resolve symlinks
    if not full_path.startswith(base_dir):
        return Response(status=403)

    # hidden files/dirs (.env, .git, ...) are never served
    if os.path.basename(full_path).startswith("."):
        return Response(status=404)

    if not os.path.isfile(full_path):
        return Response(status=404)

    stat = os.stat(full_path)
    mtime = stat.st_mtime
    size = stat.st_size

    etag = f'"{mtime}-{size}"'
    if etag == req.headers.get("if-none-match"):
        return Response(status=304)

    ct, _ = mimetypes.guess_type(full_path)

    # In-memory cache
    cache_key = full_path
    cached = _STATIC_CACHE.get(cache_key)
    if cached and cached[2] == size and cached[3] == mtime:
        data, content_type, _, _ = cached
    else:
        with open(full_path, "rb") as f:
            data = f.read()
        content_type = ct or "application/octet-stream"
        _STATIC_CACHE[cache_key] = (data, content_type, size, mtime)

    resp = Response(body=data, content_type=content_type)
    resp._headers["ETag"] = etag
    resp._headers["Cache-Control"] = f"public, max-age={cache_ttl}"
    resp._headers["Last-Modified"] = format_date_time(mtime)
    return resp
