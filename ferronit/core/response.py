"""Response objects — content-type aware, streaming-ready."""

from __future__ import annotations

import json

import msgspec

from ferronit.contrib.pydantic import encode_json
from ferronit.core.headers import HeaderInput, Headers

__all__ = ["JSONResponse", "Response", "StreamingResponse", "TextResponse"]


def _escape_html(body: bytes) -> bytes:
    """Escape ``<``, ``>``, ``&`` as ``\\uXXXX`` in an already-serialised JSON body.

    The three bytes never occur in JSON structural syntax, so a byte-level
    replace only touches string contents.
    """
    return body.replace(b"&", b"\\u0026").replace(b"<", b"\\u003c").replace(b">", b"\\u003e")


def _msgspec_json(data) -> bytes | None:
    """Encode with msgspec's C encoder.

    Returns ``None`` when msgspec refuses the data (``TypeError``), so the
    caller can fall back to the stdlib serialiser.
    """
    try:
        return _escape_html(msgspec.json.encode(data))
    except TypeError:
        return None


def _model_to_dict(data):
    """Materialise a model/dataclass into a plain dict of its fields.

    A ``rawmodel.Model`` exposes ``__columns__`` (field names in ``SELECT *``
    order); a ``msgspec.Struct`` exposes ``__struct_fields__``; a plain dataclass
    exposes ``__dataclass_fields__``. Anything else is returned unchanged.
    """
    ty = type(data)
    if hasattr(ty, "__columns__"):
        names = ty.__columns__
    elif hasattr(ty, "__struct_fields__"):
        names = ty.__struct_fields__
    elif hasattr(ty, "__dataclass_fields__"):
        names = ty.__dataclass_fields__.keys()
    else:
        return data
    return {name: getattr(data, name) for name in names}


def _json_encode(data) -> bytes:
    """Encode to JSON bytes with XSS escaping: msgspec first, stdlib fallback.

    The fallback mirrors the old Rust serialiser: unknown values become their
    ``str`` form (``default=str``), models are materialised into dicts first.
    """
    body = _msgspec_json(data)
    if body is not None:
        return body
    return _escape_html(json.dumps(_model_to_dict(data), default=str).encode("utf-8"))


class Response:
    """Base ASGI response.

    Headers are an ordered sequence of field lines: iteration over
    :attr:`headers` yields ``(name, value)`` pairs with repeats preserved, so
    several ``Set-Cookie`` lines survive to the wire.
    """

    __slots__ = ("_body", "_headers", "_status")

    def __init__(
        self,
        body: bytes | str = b"",
        status: int = 200,
        content_type: str = "text/plain",
        headers: HeaderInput = None,
    ) -> None:
        self._status = status
        self._body = body.encode() if isinstance(body, str) else body
        self._headers = Headers(headers)
        if "content-type" not in self._headers:
            self._headers._prepend("content-type", content_type)

    @property
    def headers(self) -> Headers:
        """Headers: Mutable ordered field lines; duplicates are preserved."""
        return self._headers

    def _start_message(self) -> dict:
        """Build the ASGI ``http.response.start`` message for this response."""
        return {
            "type": "http.response.start",
            "status": self._status,
            "headers": self._headers.raw(),
        }

    async def _send(self, send) -> None:
        await send(self._start_message())
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
        headers: HeaderInput = None,
    ) -> None:
        body = _json_encode(data)
        super().__init__(
            body=body,
            status=status,
            content_type="application/json; charset=utf-8",
            headers=headers,
        )

    @classmethod
    def from_model(
        cls, model, status: int = 200, headers: HeaderInput = None
    ) -> JSONResponse:
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
        headers: HeaderInput = None,
    ) -> None:
        super().__init__(status=status, content_type=content_type, headers=headers)
        self._iterator = iterator

    async def _send(self, send) -> None:
        await send(self._start_message())
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
