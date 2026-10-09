# Быстрый старт

## Установка

```bash
pip install ferrox
```

Ferrox — **один пакет**, собранный [maturin](https://www.maturin.rs)-ом как
смешанный Rust/Python-проект. Установка даёт:

- Python-слой (`ferrox`) — ASGI-движок, DDD/CQRS, DI, contrib;
- нативное ядро (`ferrox._core`) — маршрутизация, разбор запроса, JSON, gzip, CORS;
- слой данных (`ferrox.db`) — PostgreSQL → JSON целиком в Rust.

Ядро собрано с фичей `abi3-py312`, поэтому один wheel работает на любом CPython
3.12+ — матрица версий Python не нужна.

Опциональные extras:

```bash
pip install "ferrox[server]"     # granian, uvicorn
pip install "ferrox[postgres]"   # asyncpg, msgspec (сырой слой данных)
pip install "ferrox[db]"         # SQLAlchemy 2.0
pip install "ferrox[pydantic]"   # pydantic v2
```

## Первое приложение

```python
# app.py
from ferrox import Ferrox

app = Ferrox()

@app.route("/")
async def index():
    return {"service": "ferrox", "ok": True}

@app.route("/hello/{name}")
async def hello(name: str):
    return {"hello": name}
```

`app` сам является ASGI-callable — передайте его любому серверу:

```bash
granian --interface asgi --port 8000 app:app
uvicorn --port 8000 app:app
```

или используйте встроенный CLI (см. [CLI](architecture.md#cli)):

```bash
ferrox dev                       # uvicorn с авто-перезагрузкой
ferrox dev --server granian      # granian с авто-перезагрузкой
ferrox run                       # granian (fallback на uvicorn)
ferrox run --workers 4           # масштабирование по процессам
```

## Структура проекта

Обязательной структуры нет. Типичный Ferrox-сервис следует DDD-раскладке,
которую поощряет фреймворк:

```
my_service/
  app.py              # экземпляр Ferrox + HTTP-роуты (тонкие адаптеры)
  domain/             # агрегаты, сущности, доменные события (без зависимостей)
  application/        # команды, запросы, сервисы приложения
  infrastructure/     # репозитории, шины (Postgres, Kafka, ...)
```

См. [DDD, DI и гексагональная](architecture.md).

## Сборка из исходников

```bash
uv venv .venv
uv pip install -e .                     # maturin соберёт ferrox + ferrox._core
./scripts/build_packages.sh             # один wheel в dist/
```

Пересобрать только Rust-ядро во время разработки:

```bash
PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 .venv/bin/maturin develop --release
```

## Docker

```bash
docker build -t ferrox:0.8.0 .
docker run --rm -p 8000:8000 ferrox:0.8.0

# своё приложение
docker run --rm -p 8000:8000 -v "$PWD:/app" -e APP=app:app ferrox:0.8.0
```

Переменные контейнера: `APP` (`module:attr`, по умолчанию `demo_app:app`),
`SERVER` (`granian` | `uvicorn`), `HOST`, `PORT`, `WORKERS`, `RELOAD=1`.
