"""Response headers: ordered field lines with duplicates (RFC 9110 §5.2-5.3).

The wire contract is a sequence of ``(name, value)`` pairs — ``Set-Cookie`` is
allowed to repeat and must never be comma-joined, so a plain ``dict`` cannot
represent a response. Iteration over ``Response.headers`` yields those pairs in
insertion order; lookup by name stays case-insensitive and combines repeated
fields the way RFC 9110 permits.
"""

from __future__ import annotations

import asyncio

import pytest

from ferronit import JSONResponse, Response
from ferronit.core.headers import Headers


def _asgi_start(resp: Response) -> dict:
    messages: list[dict] = []

    async def send(message: dict) -> None:
        messages.append(message)

    asyncio.run(resp._send(send))
    return messages[0]


class _Proto:
    def __init__(self) -> None:
        self.status: int | None = None
        self.headers: list[tuple[str, str]] = []
        self.body: bytes = b""

    def response_bytes(self, status: int, headers, body: bytes) -> None:
        self.status = status
        self.headers = list(headers)
        self.body = body


# ── container semantics ────────────────────────────────────────────────


def test_iteration_yields_ordered_pairs_with_duplicates() -> None:
    headers = Headers(
        [("Set-Cookie", "a=1"), ("X-Tag", "x"), ("set-cookie", "b=2; Path=/")]
    )

    assert list(headers) == [
        ("set-cookie", "a=1"),
        ("x-tag", "x"),
        ("set-cookie", "b=2; Path=/"),
    ]
    assert len(headers) == 3


def test_lookup_combines_repeated_fields_but_never_set_cookie() -> None:
    headers = Headers([("X-Tag", "a"), ("x-tag", "b")])
    headers.add("Set-Cookie", "a=1")
    headers.add("Set-Cookie", "b=2")

    assert headers["X-Tag"] == "a, b"
    assert headers["set-cookie"] == "a=1"
    assert headers.get_all("Set-Cookie") == ["a=1", "b=2"]
    assert headers.get("x-missing") is None
    assert headers.get("x-missing", "-") == "-"


def test_setitem_replaces_every_line_at_the_first_position() -> None:
    headers = Headers([("X-Tag", "a"), ("Other", "v"), ("x-tag", "b")])

    headers["X-TAG"] = "z"

    assert list(headers) == [("x-tag", "z"), ("other", "v")]


def test_delitem_removes_all_matching_lines() -> None:
    headers = Headers([("Set-Cookie", "a=1"), ("set-cookie", "b=2"), ("X", "v")])

    del headers["Set-Cookie"]

    assert list(headers) == [("x", "v")]
    with pytest.raises(KeyError):
        del headers["nope"]


def test_setdefault_only_fills_absent_names() -> None:
    headers = Headers([("X", "keep")])

    headers.setdefault("x", "ignored")
    headers.setdefault("Y", "added")

    assert list(headers) == [("x", "keep"), ("y", "added")]


def test_empty_and_none_values_survive() -> None:
    headers = Headers([("X-Empty", "")])

    assert list(headers) == [("x-empty", "")]


def test_name_validation_rejects_bad_tokens() -> None:
    for name in ("", "X Bad", "X:Y", "X\r\nY", "Привет"):
        with pytest.raises(ValueError):
            Headers([(name, "v")])


def test_value_must_be_latin1() -> None:
    with pytest.raises(ValueError):
        Headers([("X-Test", "Привет")])


def test_crlf_and_nul_become_spaces() -> None:
    """RFC 9110 5.5: a recipient replaces CR, LF and NUL with SP."""
    headers = Headers([("X-Test", "v\r\nX-Injected: yes"), ("X-Other", "a\x00b")])

    assert list(headers) == [
        ("x-test", "v  X-Injected: yes"),
        ("x-other", "a b"),
    ]


# ── Response integration ───────────────────────────────────────────────


def test_response_headers_are_iterable_and_mutable() -> None:
    resp = Response(b"x", content_type="text/plain; charset=utf-8")

    resp.headers.add("Set-Cookie", "a=1")
    resp.headers["X-Tag"] = "v"

    assert list(resp.headers) == [
        ("content-type", "text/plain; charset=utf-8"),
        ("set-cookie", "a=1"),
        ("x-tag", "v"),
    ]


def test_content_type_participates_in_headers_and_can_be_overridden() -> None:
    default = Response(b"x", content_type="text/html; charset=utf-8")
    overridden = Response(
        b"x", content_type="text/html; charset=utf-8", headers=[("Content-Type", "text/csv")]
    )

    assert list(default.headers) == [("content-type", "text/html; charset=utf-8")]
    assert list(overridden.headers) == [("content-type", "text/csv")]


def test_asgi_send_keeps_two_set_cookie_and_lowercases_names() -> None:
    resp = JSONResponse(
        {"ok": True},
        headers=[("Set-Cookie", "a=1; Path=/"), ("Set-Cookie", "b=2"), ("X-Tag", "v")],
    )

    message = _asgi_start(resp)
    raw = message["headers"]

    assert raw[0] == (b"content-type", b"application/json; charset=utf-8")
    assert [k for k, _ in raw].count(b"set-cookie") == 2
    assert raw[1:] == [
        (b"set-cookie", b"a=1; Path=/"),
        (b"set-cookie", b"b=2"),
        (b"x-tag", b"v"),
    ]
    assert all(key == key.lower() for key, _ in raw)


def test_rsgi_send_keeps_two_set_cookie() -> None:
    from ferronit.core.app import _rsgi_send

    resp = JSONResponse(
        {"ok": True},
        headers=[("Set-Cookie", "a=1; Path=/"), ("Set-Cookie", "b=2")],
    )
    proto = _Proto()

    _rsgi_send(proto, resp)

    assert proto.status == 200
    assert proto.headers[0] == ("content-type", "application/json; charset=utf-8")
    assert [v for k, v in proto.headers if k == "set-cookie"] == ["a=1; Path=/", "b=2"]


def test_streaming_response_headers_follow_the_same_rules() -> None:
    from ferronit import StreamingResponse

    async def gen():
        yield b"a"

    resp = StreamingResponse(gen(), headers=[("Set-Cookie", "a=1"), ("Set-Cookie", "b=2")])
    messages: list[dict] = []

    async def send(message: dict) -> None:
        messages.append(message)

    asyncio.run(resp._send(send))

    raw = messages[0]["headers"]
    assert raw[0] == (b"content-type", b"text/event-stream")
    assert [k for k, _ in raw].count(b"set-cookie") == 2


def test_other_ctl_bytes_are_rejected() -> None:
    """CTLs other than CR/LF/NUL are invalid and have no sanctioned rewrite."""
    for bad in ("\x01", "\x0b", "\x0c", "\x1b", "\x7f"):
        with pytest.raises(ValueError):
            Headers([("X-Test", f"v{bad}rest")])


def test_ows_is_excluded_from_the_value() -> None:
    """RFC 9110 5.5: leading/trailing whitespace is not part of the value."""
    headers = Headers([("X-Pad", "  v\t "), ("X-Blank", " \t "), ("X-Inner", "a\tb")])

    assert list(headers) == [("x-pad", "v"), ("x-blank", ""), ("x-inner", "a\tb")]


def test_crlf_at_the_edge_leaves_an_empty_value() -> None:
    assert list(Headers([("X-Test", "\r\n")])) == [("x-test", "")]


def test_htab_and_obs_text_stay() -> None:
    headers = Headers([("X-Test", "a\tb"), ("X-Raw", "\xe9\xff")])

    assert list(headers) == [("x-test", "a\tb"), ("x-raw", "\xe9\xff")]
