"""Response objects — content-type aware, streaming-ready."""

from __future__ import annotations

import json as _json

from ferrox.contrib.pydantic import encode_json

__all__ = ["JSONResponse", "Response", "StreamingResponse", "TextResponse"]


def _clean_header(value: str) -> str:
    """CRLF/LF injection guard — truncate at the first line break."""
    for bad in ("\r", "\n"):
        idx = value.find(bad)
        if idx != -1:
            value = value[:idx]
    return value


class Response:
    """Base ASGI response."""

    __slots__ = ("_body", "_content_type", "_headers", "_status")

    def __init__(
        self,
        body: bytes | str = b"",
        status: int = 200,
        content_type: str = "text/plain",
        headers: dict[str, str] | None = None,
    ) -> None:
        self._status = status
        self._content_type = content_type
        self._body = body.encode() if isinstance(body, str) else body
        self._headers = headers or {}

    async def _send(self, send) -> None:
        raw_headers = [
            (b"content-type", _clean_header(self._content_type).encode("latin-1")),
        ]
        for k, v in self._headers.items():
            raw_headers.append(
                (_clean_header(k).encode("latin-1"), _clean_header(v).encode("latin-1"))
            )

        await send(
            {
                "type": "http.response.start",
                "status": self._status,
                "headers": raw_headers,
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": self._body,
            }
        )


class TextResponse(Response):
    """Plain-text response."""

    __slots__ = ()

    def __init__(self, text: str, status: int = 200) -> None:
        super().__init__(
            body=text,
            status=status,
            content_type="text/plain; charset=utf-8",
        )


class JSONResponse(Response):
    """JSON response with automatic serialisation."""

    __slots__ = ()

    def __init__(
        self,
        data: object,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        body = _json.dumps(
            data,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        super().__init__(
            body=body,
            status=status,
            content_type="application/json; charset=utf-8",
            headers=headers,
        )

    @classmethod
    def from_model(cls, model, status: int = 200, headers: dict[str, str] | None = None):
        """Encode Pydantic/dataclass model to JSON response."""
        return cls(encode_json(model), status=status, headers=headers)


class StreamingResponse(Response):
    """Server-Sent Events / chunked streaming response."""

    __slots__ = ("_iterator",)

    def __init__(
        self,
        iterator,
        status: int = 200,
        content_type: str = "text/event-stream",
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(status=status, content_type=content_type, headers=headers)
        self._iterator = iterator

    async def _send(self, send) -> None:
        raw_headers = [
            (b"content-type", _clean_header(self._content_type).encode("latin-1")),
        ]
        for k, v in self._headers.items():
            raw_headers.append(
                (_clean_header(k).encode("latin-1"), _clean_header(v).encode("latin-1"))
            )

        await send(
            {
                "type": "http.response.start",
                "status": self._status,
                "headers": raw_headers,
            }
        )
        async for chunk in self._iterator:
            if isinstance(chunk, str):
                chunk = chunk.encode()
            await send(
                {
                    "type": "http.response.body",
                    "body": chunk,
                    "more_body": True,
                }
            )
        await send(
            {
                "type": "http.response.body",
                "body": b"",
                "more_body": False,
            }
        )
