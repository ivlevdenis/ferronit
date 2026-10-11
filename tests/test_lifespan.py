"""Lifespan + DI: ресурсы в контейнере, инъекция портов по типу/Annotated."""

from __future__ import annotations

import asyncio
import json
from typing import Annotated

from ferronit import Ferronit
from ferronit.di import Container


class Repo:
    def __init__(self, token: Annotated[str, "secret"]):
        self.token = token


def _make_app(container: Container) -> Ferronit:
    app = Ferronit(container=container)
    container.factory(Repo)

    @app.route("/token")
    def token(repo: Repo):
        return {"token": repo.token}

    return app


async def _http_get(app, path: str) -> tuple[int, bytes]:
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    scope = {
        "type": "http", "method": "GET", "path": path, "query_string": b"",
        "headers": [], "scheme": "http", "server": ("test", 80),
        "client": ("1.2.3.4", 1234),
    }
    await app(scope, receive, send)
    status = sent[0]["status"]
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body


async def _wait_for(sent: list, msg_type: str) -> None:
    for _ in range(1000):
        if any(m.get("type") == msg_type for m in sent):
            return
        await asyncio.sleep(0)
    raise TimeoutError(f"{msg_type} не получен")


async def test_di_injects_repo_from_container():
    container = Container()
    container.singleton(Annotated[str, "secret"], "plain")
    app = _make_app(container)

    status, body = await _http_get(app, "/token")
    assert status == 200
    assert json.loads(body) == {"token": "plain"}


async def test_lifespan_registers_resource_and_runs_teardown():
    container = Container()
    app = _make_app(container)

    torn_down: list[bool] = []

    @app.lifespan
    async def lifespan():
        container.singleton(Annotated[str, "secret"], "started")
        yield
        torn_down.append(True)

    sent: list = []
    inbox: asyncio.Queue = asyncio.Queue()

    async def receive():
        return await inbox.get()

    async def send(msg):
        sent.append(msg)

    task = asyncio.create_task(app({"type": "lifespan"}, receive, send))

    # startup: ресурс кладётся в контейнер до startup.complete
    await inbox.put({"type": "lifespan.startup"})
    await _wait_for(sent, "lifespan.startup.complete")

    # запрос между startup и shutdown видит ресурс
    status, body = await _http_get(app, "/token")
    assert status == 200
    assert json.loads(body) == {"token": "started"}

    # shutdown: teardown отрабатывает до shutdown.complete
    await inbox.put({"type": "lifespan.shutdown"})
    await _wait_for(sent, "lifespan.shutdown.complete")
    await task
    assert torn_down == [True]
