"""Ferronit CLI — project scaffolding and dev server."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

# ── Templates ─────────────────────────────────────────────────────────

DOMAIN_TEMPLATE = '''\
"""Order aggregate — domain logic, zero dependencies."""

from ferronit.ddd import AggregateRoot


class Order(AggregateRoot):
    def __init__(self, order_id: str, user_id: str, amount: int):
        super().__init__()
        self.id = order_id
        self.user_id = user_id
        self.amount = amount
        self.status = "created"

    def cancel(self):
        if self.status == "shipped":
            raise ValueError("Cannot cancel shipped order")
        self.status = "cancelled"
'''

APP_TEMPLATE = '''\
"""Application services — use cases, no HTTP/DB code."""

from ferronit.ddd import Command, Query


class CreateOrderCmd(Command):
    __slots__ = ("order_id", "user_id", "amount")
    def __init__(self, order_id: str, user_id: str, amount: int):
        self.order_id = order_id
        self.user_id = user_id
        self.amount = amount


class GetOrderQ(Query):
    __slots__ = ("order_id",)
    def __init__(self, order_id: str):
        self.order_id = order_id


class OrderService:
    def __init__(self, repo, logger):
        self._repo = repo
        self._log = logger

    async def create(self, cmd: CreateOrderCmd):
        from domain.orders import Order
        order = Order(cmd.order_id, cmd.user_id, cmd.amount)
        await self._repo.save(order)
        await self._log.info(f"Order created: {order.id}")
        return order

    async def get(self, q: GetOrderQ):
        return await self._repo.get(q.order_id)
'''

INFRA_TEMPLATE = '''\
"""Infrastructure — DB, DI container."""

from sqlalchemy import Column, Integer, String
from ferronit.contrib.db import Base, create_relational_uow
from ferronit.di import Container
from ferronit.hexagonal import Logger, PrintLogger
from config import config


class OrderModel(Base):
    __tablename__ = "orders"
    id = Column(String, primary_key=True)
    user_id = Column(String, nullable=False)
    amount = Column(Integer, nullable=False)
    status = Column(String, default="created")


class OrderRepo:
    def __init__(self, uow):
        self._uow = uow

    async def save(self, order):
        async with self._uow:
            self._uow[OrderModel].save(order)

    async def get(self, order_id):
        async with self._uow:
            return await self._uow[OrderModel].get(order_id)


def bootstrap():
    uow = create_relational_uow(config.get("DATABASE_URL", "sqlite+aiosqlite:///db.sqlite3"))

    c = Container()
    c.singleton(Logger, PrintLogger())
    c.singleton("uow", uow)
    c.factory("OrderRepo", OrderRepo)
    c.factory("OrderService", OrderService)
    return c
'''

INTERFACES_TEMPLATE = '''\
"""HTTP interfaces — connect domain to the world."""

from ferronit import Ferronit
from ferronit.contrib.cors import cors
from ferronit.contrib.tracing import trace_middleware
from ferronit.ddd import CommandBus, QueryBus
from application.services import CreateOrderCmd, GetOrderQ
from infrastructure.db import bootstrap

container = bootstrap()
cmd_bus = CommandBus()
query_bus = QueryBus()
cmd_bus.register(CreateOrderCmd, container.get("OrderService").create)
query_bus.register(GetOrderQ, container.get("OrderService").get)

app = Ferronit()
app.use(cors())
app.use(trace_middleware)


@app.route("/")
def home(req):
    return {{"app": "%s"}}


@app.route("/orders", methods=["POST"])
async def create_order(req):
    body = await req.json()
    cmd = CreateOrderCmd(
        order_id=body["id"],
        user_id=body.get("user_id", "anonymous"),
        amount=body["amount"],
    )
    result = await cmd_bus.dispatch(cmd)
    return {{"order_id": result.id, "status": result.status}}


@app.route("/orders/{{order_id}}")
async def get_order(req):
    result = await query_bus.ask(GetOrderQ(req.params["order_id"]))
    if result is None:
        return {{"error": "not found"}}
    return {{"order_id": result.id, "status": result.status}}
'''

MAIN_TEMPLATE = '''\
"""Entry point — {name}."""

import uvicorn
from interfaces.api import app

if __name__ == "__main__":
    uvicorn.run(app, port={port})
'''

CONFIG_TEMPLATE = '''\
"""Configuration — env-based."""

from ferronit.config import EnvConfig

config = EnvConfig()
PORT = config.int("PORT", 8000)
DEBUG = config.bool("DEBUG", False)
DB_URL = config.get("DATABASE_URL", "sqlite+aiosqlite:///db.sqlite3")
'''

PYPROJECT_BASE = '''
[project]
name = "{name}"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["ferronit", "uvicorn[standard]", "sqlalchemy[asyncio]"]

[project.optional-dependencies]
dev = ["pytest", "httpx"]
granian = ["granian[reload]"]
'''

README = '''
# {name}

Ferronit DDD application.

```bash
uv pip install -e .
ferronit dev                # dev server (uvicorn, auto-reload)
ferronit dev --server granian   # dev on Granian (Rust, ~4x faster)
ferronit run                # production (Granian by default, falls back to uvicorn)
```
'''


def cmd_new(args: argparse.Namespace) -> None:
    """Scaffold a new DDD project directory.

    Creates the ``domain``, ``application``, ``infrastructure`` and
    ``interfaces`` layers together with ``pyproject.toml``, ``README.md``,
    ``config.py``, ``main.py`` and a sample Order aggregate.

    Args:
        args: Parsed CLI arguments; ``args.name`` is the project name.
    """
    name = args.name
    root = os.path.join(os.getcwd(), name)
    if os.path.exists(root):
        print(f"Error: directory '{name}' already exists", file=sys.stderr)
        sys.exit(1)

    os.makedirs(root)
    subdirs = {
        "domain": "",
        "application": "",
        "infrastructure": "",
        "interfaces": "",
    }
    for sub in subdirs:
        d = os.path.join(root, sub)
        os.makedirs(d)
        with open(os.path.join(d, "__init__.py"), "w") as f:
            f.write(f'""" {sub} layer. """\n')

    # pyproject.toml
    with open(os.path.join(root, "pyproject.toml"), "w") as f:
        f.write(PYPROJECT_BASE.format(name=name))

    with open(os.path.join(root, "README.md"), "w") as f:
        f.write(README.format(name=name))

    # config.py
    with open(os.path.join(root, "config.py"), "w") as f:
        f.write(CONFIG_TEMPLATE)

    # Domain — Orders aggregate
    with open(os.path.join(root, "domain", "orders.py"), "w") as f:
        f.write(DOMAIN_TEMPLATE)

    # Application — OrderService
    with open(os.path.join(root, "application", "services.py"), "w") as f:
        f.write(APP_TEMPLATE)

    # Infrastructure — DB + DI
    with open(os.path.join(root, "infrastructure", "db.py"), "w") as f:
        f.write(INFRA_TEMPLATE)

    # Interfaces — HTTP
    with open(os.path.join(root, "interfaces", "api.py"), "w") as f:
        f.write(INTERFACES_TEMPLATE.format(name=name))

    # main.py
    with open(os.path.join(root, "main.py"), "w") as f:
        f.write(MAIN_TEMPLATE.format(name=name, port=8000))

    print(f"Created {name}/")
    print(f"  cd {name} && pip install -e . && python main.py")


def cmd_dev(args: argparse.Namespace) -> None:
    """Run the development server with auto-reload.

    Uses uvicorn unless ``--server granian`` is requested. The ASGI app
    module is auto-detected via :func:`_find_app` when ``--app`` is omitted.

    Args:
        args: Parsed CLI arguments (``port``, ``host``, ``app``, ``server``).
    """
    port = args.port or 8000
    host = args.host or "127.0.0.1"
    # Find app.py in current dir or ./name/app.py
    app_module = args.app or _find_app()
    if not app_module:
        print("No app.py found. Run from project root or pass --app", file=sys.stderr)
        sys.exit(1)

    server = (args.server or "uvicorn").lower()
    print(f"Running {app_module} on http://{host}:{port} ({server})")
    if server == "granian":
        _check_granian()
        subprocess.run(
            [sys.executable, "-m", "granian", "--interface", "asgi",
             "--host", host, "--port", str(port), "--reload", app_module],
            check=False,
        )
    else:
        subprocess.run(
            [sys.executable, "-m", "uvicorn", f"{app_module}",
             "--host", host, "--port", str(port), "--reload"],
            check=False,
        )


def cmd_run(args: argparse.Namespace) -> None:
    """Production server — defaults to Granian (Rust, ~4x faster than uvicorn)."""
    port = args.port or 8000
    host = args.host or "0.0.0.0"
    workers = args.workers or 1
    app_module = args.app or _find_app()
    if not app_module:
        print("No app.py found. Run from project root or pass --app", file=sys.stderr)
        sys.exit(1)

    server = (args.server or "granian").lower()
    print(f"Running {app_module} on http://{host}:{port} ({server}, {workers} worker(s))")
    if server == "granian":
        if _check_granian(exit_on_missing=False) is None:
            print("Granian not installed — falling back to uvicorn. Install: uv pip install granian", file=sys.stderr)
            server = "uvicorn"
        else:
            subprocess.run(
                [sys.executable, "-m", "granian", "--interface", "asgi",
                 "--host", host, "--port", str(port), "--workers", str(workers), app_module],
                check=False,
            )
            return
    subprocess.run(
        [sys.executable, "-m", "uvicorn", f"{app_module}",
         "--host", host, "--port", str(port), "--workers", str(workers)],
        check=False,
    )


def _check_granian(exit_on_missing: bool = True):
    try:
        import granian
        return granian
    except ImportError:
        if exit_on_missing:
            print("Granian not installed. Install: uv pip install granian", file=sys.stderr)
            sys.exit(1)
        return None


def _find_app() -> str | None:
    cwd = os.path.basename(os.getcwd())
    if os.path.exists(f"{cwd}/app.py"):
        return f"{cwd}.app:app"
    if os.path.exists("app.py"):
        return "app:app"
    return None


def main() -> None:
    """Parse CLI arguments and dispatch to the selected subcommand.

    Registers the ``new``, ``dev`` and ``run`` subcommands; when no
    subcommand is given it prints help and exits.
    """
    parser = argparse.ArgumentParser("ferronit", description="Ferronit CLI")
    sub = parser.add_subparsers(dest="command")

    # ferronit new NAME
    p = sub.add_parser("new", help="Scaffold a new project")
    p.add_argument("name", help="Project name")
    p.add_argument("--ddd", action="store_true", help="Include DDD example")
    p.set_defaults(func=cmd_new)

    # ferronit dev
    p = sub.add_parser("dev", help="Run development server (auto-reload)")
    p.add_argument("--port", type=int, help="Port (default: 8000)")
    p.add_argument("--host", help="Host (default: 127.0.0.1)")
    p.add_argument("--app", help="Module path (default: auto-detect)")
    p.add_argument("--server", choices=["uvicorn", "granian"], help="Server backend (default: uvicorn)")
    p.set_defaults(func=cmd_dev)

    # ferronit run
    p = sub.add_parser("run", help="Run production server (no reload)")
    p.add_argument("--port", type=int, help="Port (default: 8000)")
    p.add_argument("--host", help="Host (default: 0.0.0.0)")
    p.add_argument("--app", help="Module path (default: auto-detect)")
    p.add_argument("--server", choices=["uvicorn", "granian"], help="Server backend (default: granian)")
    p.add_argument("--workers", type=int, help="Worker processes (default: 1)")
    p.set_defaults(func=cmd_run)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)
    args.func(args)


if __name__ == "__main__":
    main()
