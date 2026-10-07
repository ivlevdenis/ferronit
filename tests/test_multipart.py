"""`Request.form()` / `Request.files()`: urlencoded и multipart/form-data."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from ferrox import Ferrox


@pytest.fixture
def app():
    v = Ferrox()

    @v.route("/upload", methods=["POST"])
    async def upload(req):
        fields = await req.form()
        files = await req.files()
        uploaded = files.get("file", [])
        return {
            "title": fields.get("title", [None])[0],
            "files": [
                {"filename": f.filename, "content_type": f.content_type, "content": f.content.decode()}
                for f in uploaded
            ],
        }

    @v.route("/form", methods=["POST"])
    async def form_only(req):
        return {"fields": await req.form()}

    @v.route("/files", methods=["POST"])
    async def files_only(req):
        files = await req.files()
        return {"count": sum(len(v) for v in files.values())}

    @v.route("/meta", methods=["POST"])
    async def meta(req):
        files = await req.files()
        return {
            "files": [
                {"filename": f.filename, "size": f.size, "content_type": f.content_type}
                for f in files.get("file", [])
            ]
        }

    return v


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def multipart_body(boundary: str, parts: list[tuple[bytes, bytes]]) -> bytes:
    """Собираем multipart-тело вручную — независимо от квирков httpx."""
    out = b""
    for headers, content in parts:
        out += b"--" + boundary.encode() + b"\r\n" + headers + b"\r\n\r\n" + content + b"\r\n"
    out += b"--" + boundary.encode() + b"--\r\n"
    return out


async def test_multipart_file_and_field(client) -> None:
    r = await client.post(
        "/upload",
        data={"title": "hello"},
        files={"file": ("note.txt", b"some bytes", "text/plain")},
    )
    assert r.status_code == 200
    assert r.json() == {
        "title": "hello",
        "files": [{"filename": "note.txt", "content_type": "text/plain", "content": "some bytes"}],
    }


async def test_multipart_multiple_files(client) -> None:
    body = multipart_body("testboundary", [
        (b'Content-Disposition: form-data; name="file"; filename="a.txt"\r\nContent-Type: text/plain', b"1"),
        (b'Content-Disposition: form-data; name="file"; filename="b.txt"\r\nContent-Type: text/plain', b"22"),
    ])
    r = await client.post(
        "/upload", content=body, headers={"content-type": "multipart/form-data; boundary=testboundary"}
    )
    assert r.status_code == 200
    assert r.json()["files"] == [
        {"filename": "a.txt", "content_type": "text/plain", "content": "1"},
        {"filename": "b.txt", "content_type": "text/plain", "content": "22"},
    ]


async def test_urlencoded_form(client) -> None:
    r = await client.post(
        "/form", content="a=1&b=2&a=3",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert r.json() == {"fields": {"a": ["1", "3"], "b": ["2"]}}


async def test_form_rejects_json_content_type(client) -> None:
    r = await client.post("/form", json={"a": 1})
    assert r.status_code == 415


async def test_files_rejects_non_multipart(client) -> None:
    r = await client.post("/files", content=b"plain text", headers={"content-type": "text/plain"})
    assert r.status_code == 415


async def test_uploaded_file_size(client) -> None:
    r = await client.post("/meta", files={"file": ("a.bin", b"12345", "application/octet-stream")})
    assert r.json()["files"] == [
        {"filename": "a.bin", "size": 5, "content_type": "application/octet-stream"}
    ]


def test_uploaded_file_save(tmp_path) -> None:
    from ferrox import UploadedFile

    upload = UploadedFile(filename="a.txt", content=b"hello", content_type="text/plain")
    destination = tmp_path / "out.txt"
    upload.save(destination)
    assert destination.read_bytes() == b"hello"
