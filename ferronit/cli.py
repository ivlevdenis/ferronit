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
from ferronit.hexagonal import Logger
from infrastructure.db import OrderRepo


class CreateOrderCmd(Command):
    order_id: str
    user_id: str
    amount: int


class GetOrderQ(Query):
    order_id: str


class OrderService:
    def __init__(self, repo: OrderRepo, logger: Logger):
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
"""Infrastructure — DB models and repositories."""

from typing import Annotated

from ferronit.contrib.rawdb import Pool
from ferronit.contrib.rawmodel import Model

# Маркер пула: разные БД — разные Annotated-ключи.
ShopPool = Annotated[Pool, "shop"]


class OrderModel(Model):
    __table__ = "orders"
    id: str = ""
    user_id: str = ""
    amount: int = 0
    status: str = "created"


class OrderRepo:
    def __init__(self, pool: ShopPool):
        self._pool = pool

    async def save(self, order):
        async with self._pool.uow() as uow:
            await uow.model(OrderModel).save(OrderModel(
                id=order.id, user_id=order.user_id, amount=order.amount, status=order.status,
            ))

    async def get(self, order_id):
        async with self._pool.uow(readonly=True) as uow:
            return await uow.model(OrderModel).get(order_id)
'''

INTERFACES_TEMPLATE = '''\
"""HTTP interfaces — connect domain to the world."""

from ferronit import Ferronit
from ferronit.contrib.cors import cors
from ferronit.contrib.rawdb import create_pool
from ferronit.contrib.tracing import trace_middleware
from ferronit.di import Container
from ferronit.hexagonal import Logger, PrintLogger
from application.services import CreateOrderCmd, GetOrderQ, OrderService
from config import config
from infrastructure.db import OrderRepo, ShopPool

container = Container()
container.singleton(Logger, PrintLogger())
container.factory(OrderRepo)
container.factory(OrderService)

app = Ferronit(container=container)
app.use(cors())
app.use(trace_middleware)


@app.lifespan
async def lifespan():
    pool = await create_pool(
        config.get("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5432/postgres"),
        min_size=1,
        max_size=16,
    )
    container.singleton(ShopPool, pool)
    async with pool.uow() as uow:
        await uow.connection.execute(
            "CREATE TABLE IF NOT EXISTS orders ("
            "id text PRIMARY KEY, user_id text NOT NULL, amount integer NOT NULL, "
            "status text DEFAULT 'created')"
        )
    yield
    await pool.close()


@app.route("/")
def home(req):
    return {{"app": "{name}"}}


@app.route("/orders", methods=["POST"])
async def create_order(req, orders: OrderService):
    body = await req.json()
    cmd = CreateOrderCmd(
        order_id=body["id"],
        user_id=body.get("user_id", "anonymous"),
        amount=body["amount"],
    )
    result = await orders.create(cmd)
    return {{"order_id": result.id, "status": result.status}}


@app.route("/orders/{{order_id}}")
async def get_order(req, orders: OrderService):
    result = await orders.get(GetOrderQ(req.params["order_id"]))
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

MINIMAL_APP_TEMPLATE = '''\
"""Minimal Ferronit application."""

from ferronit import Ferronit

app = Ferronit()


@app.route("/")
async def index():
    return {{"service": "{name}", "ok": True}}
'''

MINIMAL_MAIN_TEMPLATE = '''\
"""Entry point — {name}."""

import uvicorn
from app import app

if __name__ == "__main__":
    uvicorn.run(app, port={port})
'''

CONFIG_TEMPLATE = '''\
"""Configuration — env-based."""

from ferronit.config import EnvConfig

config = EnvConfig()
PORT = config.int("PORT", 8000)
DEBUG = config.bool("DEBUG", False)
DB_URL = config.get("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5432/postgres")
'''

PYPROJECT_BASE = '''
[project]
name = "{name}"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["ferronit", "uvicorn[standard]"]

[project.optional-dependencies]
dev = ["pytest", "httpx"]
granian = ["granian[reload]"]
'''

README = '''
# {name}

Ferronit {kind} application.

```bash
uv pip install -e .
ferronit dev                # dev server (uvicorn, auto-reload)
ferronit dev --server granian   # dev on Granian (Rust, ~4x faster)
ferronit run                # production (Granian by default, falls back to uvicorn)
```
'''


def cmd_new(args: argparse.Namespace) -> None:
    """Scaffold a new project directory.

    By default generates a minimal single-file application (``app.py`` +
    ``main.py``). With ``--ddd`` it creates the full DDD layout — ``domain``,
    ``application``, ``infrastructure`` and ``interfaces`` layers, a sample
    Order aggregate and ``config.py`` — plus ``pyproject.toml`` and
    ``README.md`` in both cases.

    Args:
        args: Parsed CLI arguments; ``args.name`` is the project name and
            ``args.ddd`` selects the DDD template.
    """
    name = args.name
    root = os.path.join(os.getcwd(), name)
    if os.path.exists(root):
        print(f"Error: directory '{name}' already exists", file=sys.stderr)
        sys.exit(1)

    os.makedirs(root)

    # Общие файлы для обоих шаблонов.
    with open(os.path.join(root, "pyproject.toml"), "w") as f:
        f.write(PYPROJECT_BASE.format(name=name))
    with open(os.path.join(root, "README.md"), "w") as f:
        f.write(README.format(name=name, kind="DDD" if args.ddd else "minimal"))

    if not args.ddd:
        # Минимальный каркас: один app.py + entry point.
        with open(os.path.join(root, "app.py"), "w") as f:
            f.write(MINIMAL_APP_TEMPLATE.format(name=name))
        with open(os.path.join(root, "main.py"), "w") as f:
            f.write(MINIMAL_MAIN_TEMPLATE.format(name=name, port=8000))
    else:
        # Полная DDD-раскладка + пример агрегата Order.
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

        with open(os.path.join(root, "config.py"), "w") as f:
            f.write(CONFIG_TEMPLATE)
        with open(os.path.join(root, "domain", "orders.py"), "w") as f:
            f.write(DOMAIN_TEMPLATE)
        with open(os.path.join(root, "application", "services.py"), "w") as f:
            f.write(APP_TEMPLATE)
        with open(os.path.join(root, "infrastructure", "db.py"), "w") as f:
            f.write(INFRA_TEMPLATE)
        with open(os.path.join(root, "interfaces", "api.py"), "w") as f:
            f.write(INTERFACES_TEMPLATE.format(name=name))
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
