"""Set-Cookie: Rust serialisation, RFC 6265 validation, duplicates on the wire."""

from __future__ import annotations

import asyncio

import pytest

from ferronit import JSONResponse, Response


def _asgi_start(resp: Response) -> list[tuple[bytes, bytes]]:
    messages: list[dict] = []

    async def send(message: dict) -> None:
        messages.append(message)

    asyncio.run(resp._send(send))
    return messages[0]["headers"]


def _cookies(resp: Response) -> list[str]:
    return resp.headers.get_all("set-cookie")


class _Proto:
    def __init__(self) -> None:
        self.status: int | None = None
        self.headers: list[tuple[str, str]] = []
        self.body: bytes = b""

    def response_bytes(self, status: int, headers, body: bytes) -> None:
        self.status = status
        self.headers = list(headers)
        self.body = body


def test_set_cookie_appends_instead_of_replacing() -> None:
    resp = JSONResponse({"ok": True})

    resp.set_cookie("a", "1")
    resp.set_cookie("b", "2")

    assert _cookies(resp) == [
        "a=1; Path=/; SameSite=Lax",
        "b=2; Path=/; SameSite=Lax",
    ]
    raw = _asgi_start(resp)
    assert [v for k, v in raw if k == b"set-cookie"] == [
        b"a=1; Path=/; SameSite=Lax",
        b"b=2; Path=/; SameSite=Lax",
    ]


def test_rsgi_send_keeps_both_cookies() -> None:
    from ferronit.core.app import _rsgi_send

    resp = JSONResponse({"ok": True})
    resp.set_cookie("a", "1", path="/x")
    resp.set_cookie("b", "2")
    proto = _Proto()

    _rsgi_send(proto, resp)

    assert [v for k, v in proto.headers if k == "set-cookie"] == [
        "a=1; Path=/x; SameSite=Lax",
        "b=2; Path=/; SameSite=Lax",
    ]


def test_all_attributes_are_serialised_in_order() -> None:
    resp = Response(b"ok")

    resp.set_cookie(
        "sid",
        "abc",
        expires="Wed, 21 Oct 2026 07:28:00 GMT",
        max_age=60,
        domain="example.com",
        path="/api",
        secure=True,
        http_only=True,
        same_site="strict",
    )

    assert _cookies(resp) == [
        "sid=abc; Expires=Wed, 21 Oct 2026 07:28:00 GMT; Max-Age=60; "
        "Domain=example.com; Path=/api; Secure; HttpOnly; SameSite=Strict"
    ]


def test_same_site_is_case_insensitive_and_defaults_to_lax() -> None:
    resp = Response(b"ok")

    resp.set_cookie("a", "1")
    resp.set_cookie("b", "2", same_site="NONE")
    resp.set_cookie("c", "3", same_site=None)

    assert _cookies(resp) == [
        "a=1; Path=/; SameSite=Lax",
        "b=2; Path=/; SameSite=None",
        "c=3; Path=/",
    ]


def test_delete_cookie_expires_immediately() -> None:
    resp = Response(b"ok")

    resp.delete_cookie("sid", path="/app")

    assert _cookies(resp) == [
        "sid=; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Max-Age=0; Path=/app"
    ]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "bad name"},
        {"name": "bad;name"},
        {"name": ""},
        {"name": "sid", "value": "a b"},
        {"name": "sid", "value": "a;b"},
        {"name": "sid", "value": "a,b"},
        {"name": "sid", "value": "a\r\nX: y"},
        {"name": "sid", "value": "привет"},
        {"name": "sid", "value": 'a"b'},
        {"name": "sid", "value": "a\\b"},
        {"name": "sid", "path": "/a;b"},
        {"name": "sid", "path": "/a\r\nX: y"},
        {"name": "sid", "domain": "exa mple.com"},
        {"name": "sid", "expires": "Wed, 21 Oct 2026; X=1"},
        {"name": "sid", "same_site": "sometimes"},
    ],
)
def test_invalid_parts_are_rejected(kwargs: dict) -> None:
    resp = Response(b"ok")

    with pytest.raises(ValueError):
        resp.set_cookie(**kwargs)
    assert _cookies(resp) == []
