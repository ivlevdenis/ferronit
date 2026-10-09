# Быстрый старт

## Что это

Ferronit — ASGI-фреймворк, у которого «дорогие» части запроса вынесены в Rust.
Python остаётся только там, где он нужен — в ваших хендлерах и бизнес-логике.
За счёт этого на пустых маршрутах он держит ~96 000 req/s (Granian) и не
деградирует с ростом числа маршрутов — см. [бенчмарки](benchmarks.md).

## Установка

```bash
pip install ferronit
```

Ferronit — **один пакет**, собранный [maturin](https://www.maturin.rs)-ом как
смешанный Rust/Python-проект. Одна установка даёт:

- Python-слой (`ferronit`) — ASGI-движок, DDD/CQRS, DI, contrib;
- нативное ядро (`ferronit._core`) — маршрутизация, разбор запроса, JSON, gzip, CORS;
- слой данных (`ferronit.db`) — запросы PostgreSQL → JSON целиком в Rust.

Ядро собрано с фичей `abi3-py312`, поэтому один wheel работает на любом
CPython 3.12+ — матрица версий Python не нужна. Внешних runtime-зависимостей
у ядра нет.

Опциональные extras (только то, что реально используете):

```bash
pip install "ferronit[server]"     # granian, uvicorn — для запуска
pip install "ferronit[postgres]"   # asyncpg, msgspec — сырой слой данных
pip install "ferronit[db]"         # SQLAlchemy 2.0 + aiosqlite (асинхронный SQLite)
pip install "ferronit[pydantic]"   # pydantic v2
```

## Первое приложение

```python
# app.py
from ferronit import Ferronit

app = Ferronit()

@app.route("/")
async def index():
    return {"service": "ferronit", "ok": True}

@app.route("/hello/{name}")
async def hello(name: str):
    return {"hello": name}
```

`app` сам является ASGI-callable — передайте его любому серверу:

```bash
granian --interface asgi --port 8000 app:app
uvicorn --port 8000 app:app
```

или используйте встроенный CLI:

```bash
ferronit dev                       # uvicorn с авто-перезагрузкой
ferronit dev --server granian      # granian с авто-перезагрузкой
ferronit run                       # granian (fallback на uvicorn)
ferronit run --workers 4           # масштабирование по процессам
```

## Генерация проекта

```bash
ferronit new my_service
```

Создаёт DDD-проект: `domain/` (агрегаты), `application/` (сервисы и CQRS),
`infrastructure/` (репозитории, DI), `interfaces/` (HTTP-роуты) — плюс
`config.py`, `main.py` и `pyproject.toml`. Дальше:

```bash
cd my_service && pip install -e . && python main.py
```

## Структура проекта

Обязательной структуры нет. Фреймворк поощряет DDD-раскладку, но не навязывает —
`app = Ferronit()` в одном файле работает так же хорошо:

```
my_service/
  app.py              # экземпляр Ferronit + HTTP-роуты (тонкие адаптеры)
  domain/             # агрегаты, сущности, доменные события (без зависимостей)
  application/        # команды, запросы, сервисы приложения
  infrastructure/     # репозитории, шины (Postgres, Kafka, ...)
```

См. [DDD, DI и гексагональная](architecture.md).

## Docker

Образ самодостаточный: ядро компилируется maturin-ом внутри билд-стейджа,
наружу идёт `python:3.12-slim` с одним wheel-ом и прод-сервером Granian. Ни
ABI, ни Rust на машине для запуска не нужны.

```bash
docker build -t ferronit:0.9.0 .
docker run --rm -p 8000:8000 ferronit:0.9.0

# своё приложение
docker run --rm -p 8000:8000 -v "$PWD:/app" -e APP=app:app ferronit:0.9.0
```

Переменные контейнера: `APP` (`module:attr`, по умолчанию `demo_app:app`),
`SERVER` (`granian` | `uvicorn`), `HOST`, `PORT`, `WORKERS`, `RELOAD=1`.

## Сборка из исходников

```bash
poetry install --all-extras             # окружение + зависимости
PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 poetry run maturin develop --release   # Rust-ядро
./scripts/build_packages.sh             # wheel + sdist в dist/
```

Пересобрать только Rust-ядро во время разработки:

```bash
PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 poetry run maturin develop --release
```

## Что дальше

- [Маршрутизация](routing.md) → [Запросы](requests.md) → [Ответы](responses.md)
- [Слой данных](data/overview.md) — когда какой из трёх слоёв брать
