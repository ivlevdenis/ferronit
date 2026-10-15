# Ferronit

[English](README.md) · **Русский**

[![PyPI](https://img.shields.io/pypi/v/ferronit)](https://pypi.org/project/ferronit/)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](https://pypi.org/project/ferronit/)
[![License](https://img.shields.io/pypi/l/ferronit)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/ivlevdenis/ferronit)](https://github.com/ivlevdenis/ferronit/stargazers)

> ⚠️ **Ранняя разработка — код не готов к продакшен.** API и внутренности ещё
> меняются, используйте на свой страх и риск.

Быстрая замена легаси-кода, который не тянет нагрузку: маршрутизация, разбор запроса,
JSON, gzip, CORS и PostgreSQL уходят в Rust-ядро, а ваши хендлеры остаются обычным Python.
Миграция, а не переписывание.

**Документация:** [`docs/`](docs/README.md) — быстрый старт, роутинг, запросы,
инъекция, слой данных (rawdb + rawmodel), DDD/DI, Rust-ядро, бенчмарки, ASVS.

## Установка

```bash
pip install ferronit        # ставит сразу Python-слой и нативное ядро
```

Ferronit — **один пакет**, собранный [maturin](https://www.maturin.rs)-ом как
mixed-проект. Ядро (`ferronit._core`) собирается с фичей `abi3-py312` — один wheel
работает на всех CPython от 3.12 до 3.15+. Обязательные runtime-зависимости —
`msgspec` (быстрый JSON/валидация) и `asyncpg` (слой данных rawdb/rawmodel).

```python
# app.py
from ferronit import Ferronit

app = Ferronit()

@app.route("/hello/{name}")
async def hello(name: str):
    return {"hello": name}
```

```bash
ferronit dev                          # dev-сервер (uvicorn, reload)
ferronit dev --server granian         # granian (Rust, ~4× быстрее)
ferronit run --workers 4              # прод: granian, fallback uvicorn
```

## Почему быстро

Маршрутизация — Rust `matchit`, фактически O(1). На чтении из БД Ferronit обходит
конкурентов позиционным маппингом `rawmodel` + `msgspec`. Методика — только `ab`
(C-клиент), granian, 1 воркер; полный разбор — [`docs/benchmarks.md`](docs/benchmarks.md).

| Сценарий | Ferronit | BlackSheep | FastAPI |
|---|---|---|---|
| GET /ping (dict) | 98 197 req/s | 93 523 req/s | 19 036 req/s |
| GET /rows?limit=100 | 8 350 req/s | 8 065 req/s | 956 req/s |
| GET /rows?limit=1000 | 2 334 req/s | 1 780 req/s | 158 req/s |

## Слой данных

Один тонкий слой поверх драйвера — `rawdb` + `rawmodel`: PostgreSQL (asyncpg), MySQL (aiomysql)
и SQLite (aiosqlite) за одним API. Декларативные модели, query DSL (`select`), связи, Unit of Work —
без ORM. SQL собирается деревом выражений и компилируется под диалект. Подробнее:
[`docs/data/overview.md`](docs/data/overview.md).

```python
from typing import Annotated
from ferronit import Ferronit
from ferronit.contrib.rawdb import create_pool
from ferronit.contrib.rawmodel import Model, Relation

class OrderItem(Model):
    __table__ = "order_items"
    id: int = 0
    order_id: int = 0
    name: str = ""

class Order(Model):
    __table__ = "orders"
    id: int = 0
    items: Annotated[list[OrderItem], Relation("order_id")] = []   # 1:N, грузится всегда

app = Ferronit()

@app.route("/orders")
async def orders():
    pool = await create_pool("postgresql://user:pass@localhost/db")  # обычно из lifespan/DI
    async with pool.uow(readonly=True) as uow:
        rows = await uow.model(Order).list(limit=10)
    return {"orders": rows}   # items вложенно в JSON, один WHERE fk = ANY($1)
```

## Разработка

```bash
poetry install --all-extras             # окружение в .venv + все зависимости
PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 poetry run maturin develop --release   # Rust-ядро
./scripts/check.sh                      # линт → типы → докстринги → сборка ядра → тесты
poetry run pytest tests/ -q             # 265 тестов
```

Окружение ведёт Poetry (`poetry.toml` — venv внутри проекта), wheel с Rust-ядром собирает
maturin (см. `[tool.maturin]` в `pyproject.toml`), поэтому сам проект Poetry не ставит.

- **[AGENTS.md](AGENTS.md)** — инструкция для код-агентов (Claude Code, Codex, Cursor, ...).
- **`examples/`** — minimal, DI, LLM + SSE, RAG, WebSocket; `examples/app.py` — DDD-пример.
- Пакет помечен `py.typed`; для Rust-ядра лежит `ferronit/_core.pyi`.
- `bench/` — измерительный стенд (все цифры выше), не входит в пакет.

## Релиз

```bash
# версия правится в двух файлах: ferronit/__init__.py и ferronit-rs/Cargo.toml
git tag -a v0.19.1 -m "Ferronit 0.19.1" && git push origin v0.19.1
```

Тег `v*` запускает `.github/workflows/release.yml`: проверки и тесты → колёса (Linux glibc/musl,
macOS, Windows) и sdist → GitHub Release с артефактами → публикация на PyPI (API-токен из
секрета репозитория `PYPI_API_TOKEN`).
Если версия тега не совпадает с `Cargo.toml`/`__init__.py`, workflow падает до публикации.

## Docker

```bash
docker build -t ferronit:0.19.1 .
docker run --rm -p 8000:8000 ferronit:0.19.1
```

## Лицензия

Apache License 2.0 — см. [LICENSE](LICENSE). Использование, изменение и распространение
разрешены, включая коммерческое; сохраняются уведомления об авторстве и патентная оговорка.
