# AGENTS.md — Ferronit

Инструкция для код-агентов (Claude Code, Codex, Cursor, Copilot, Pi, Gemini CLI и др.).
Прочитай целиком до первого изменения кода.

## Что это

Ferronit — высокопроизводительный Python ASGI-фреймворк, у которого инфраструктурный слой
написан на Rust: **роутинг, парсинг запроса, JSON-сериализация, gzip и CORS**. Бизнес-логика
остаётся обычным Python: DDD / CQRS, hexagonal (порты и адаптеры), DI-контейнер.

Версия 0.9.0, требуется Python ≥ 3.12. Скорость: 88 768 req/s на Granian (×8.0 к FastAPI
на том же коде и железе), полный hot path ~11 мкс на запрос.

Репозиторий — **один дистрибутив** `ferronit` (maturin mixed):

| Каталог | Что это |
|---|---|
| `ferronit/` | Python-пакет: ASGI-движок, DDD, DI, contrib + нативное ядро `ferronit._core` |
| `ferronit-rs/` | Rust-крейт (роутинг/JSON/gzip/CORS + `db`), собирается как `ferronit._core` |
| `ferronit/db.py` | Слой данных `ferronit.db` (`connect` / `query_json` → `ferronit._core.db`) |

Сборка — maturin (`abi3-py312`), один wheel: `pip install ferronit` ставит сразу Python-слой и ядро.

## Карта репозитория

```
ferronit/
├── __init__.py            # Ferronit, Request, Response, WebSocket, __version__
├── core/
│   ├── app.py             # ASGI-движок: маршруты, middleware, gzip, CORS, lifespan
│   ├── request.py         # Request (заголовки/query в Rust), PathParamError, BodyTooLarge
│   └── response.py        # Response, JSONResponse, StreamingResponse, TextResponse
├── routing.py             # RouteNode + независимая от CLI регистрация маршрутов
├── injection.py           # auto-injection параметров (стратегия выбирается при регистрации)
├── middleware.py          # стек middleware
├── di.py                  # Container: синглтоны/фабрики по типу или строке
├── ddd.py                 # AggregateRoot, Command/Query, CommandBus, Repository
├── hexagonal.py           # Port, Adapter, UnitOfWork, ApplicationService + Logger/Cache/EventBus
├── openapi.py             # генерация OpenAPI из type hints
├── websocket.py           # WebSocket-хендлеры, Origin-guard
├── config.py              # EnvConfig: типизированные переменные окружения
├── cli.py                 # ferronit new / dev / run + шаблоны scaffold-проекта
└── contrib/               # инфраструктурные адаптеры (опциональные зависимости)
    ├── db.py              # SQLAlchemy: RelationalRepository/UnitOfWork, CoreRepository
    ├── rawdb.py           # тонкий слой на asyncpg: RawRepository/RawUnitOfWork (без ORM)
    ├── rawmodel.py        # декларативные модели + Query/select поверх rawdb (asyncpg)
    ├── llm.py             # LlmPort + OpenAiAdapter, ClaudeAdapter, MockLlmAdapter
    ├── vectordb.py        # VectorDbPort + Qdrant, Chroma, Mock
    ├── kafka.py           # KafkaMessageBus (aiokafka)
    ├── cors.py, security.py, ratelimit.py, staticfiles.py, tracing.py, health.py
    └── pydantic/          # кодек pydantic v2

ferronit-rs/src/lib.rs        # Rust: Router, parse_headers, Response::json (в т.ч. модели), gzip, CORS
ferronit-rs/src/db.rs         # Rust: PostgreSQL → JSON (deadpool-postgres + prepare_cached, маппинг типов)
ferronit/db.py                # слой данных ferronit.db (connect/query_json → ferronit._core.db)
pyproject.toml                # метаданные, extras, [tool.poetry], [tool.maturin], ruff/mypy
poetry.toml, poetry.lock      # окружение Poetry (venv в .venv/) и зафиксированные зависимости
bench/                     # измерительный стенд (все цифры — docs/benchmarks.md) + README.md внутри
tests/                     # 263 теста; tests/security/ — 64 (в т.ч. 14 ASVS L1)
examples/                  # минимальный, DI, LLM+SSE, RAG, WebSocket + README
docker/                    # Dockerfile-обвязка: demo_app.py, entrypoint.sh
docs/                      # документация: README.md (хаб), data/ (слой данных), benchmarks.md,
                           #   security/ASVS.md (OWASP ASVS 5.0 L1)
```

Бенчмарк-скрипты лежат в `bench/` (см. `bench/README.md`) — **измерительный стенд, а не часть
пакета**: не рефактори их, не линтуй (каталог исключён из ruff и Docker), не переноси обратно в
корень. **req/s по HTTP меряем только `ab`** (`./bench/run_all_ab.sh` → `bench/RESULTS_ab.txt`):
httpx-скрипты убраны в `bench/legacy_httpx/`, потому что Python-клиент упирается в ~3.7k req/s
и измеряет сам себя. Helper-приложения (`bench/_bench_*.py`) запускаются основными скриптами
динамически (`f"_bench_pool_{name}.py"`), поэтому поиск по имени их «не находит» — не удаляй их,
не проверив `bench/bench_*.py`.

## Команды

```bash
# установка для разработки (Poetry ведёт окружение, maturin собирает Rust-ядро)
poetry install --all-extras                              # venv в .venv/ + зависимости
PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 poetry run maturin develop --release   # ядро

# всё сразу: линт → типы → докстринги → сборка ядра → тесты
./scripts/check.sh

# по отдельности (можно и .venv/bin/... — venv лежит внутри проекта)
poetry run pytest tests/ -q                    # 263 теста, ~4 с
poetry run pytest tests/security -q            # security-набор + ASVS L1
poetry run ruff check ferronit tests           # линт
poetry run mypy ferronit                       # типы
poetry run python scripts/agent_readiness.py   # покрытие докстрингами (сейчас 100%)
poetry run python scripts/agent_readiness.py --missing   # что конкретно без докстринга

# только Rust-ядро (после правок в ferronit-rs/)
PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 poetry run maturin develop --release

# wheel + sdist в dist/
./scripts/build_packages.sh

# прод и dev
poetry run ferronit dev --server granian   # dev-сервер с reload
poetry run ferronit run --workers 4        # прод: Granian, fallback uvicorn

# docker
docker build -t ferronit:0.9.0 . && ./scripts/docker_smoke.sh
```

Тесты Postgres (`tests/test_postgres_real.py`, 4 шт.) требуют контейнер на :5432:

```bash
docker start ferronit-pg   # если контейнера нет:
docker run -d --name ferronit-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_USER=postgres \
  -e POSTGRES_DB=postgres -p 5432:5432 postgres:18
```

## Инварианты — НЕ ломать

1. **Версии синхронизированы**: `ferronit/__init__.py::__version__` == `ferronit-rs/Cargo.toml::version`
   == метаданные дистрибутива `ferronit`. Ловится `tests/test_packaging.py`; при выпуске править **два** файла.
2. **`Cargo.lock` в репозитории** — фичи pyo3 менять только через `Cargo.toml` + пересборку.
3. **`abi3-py312`, не понижать**: `PyString::to_str` требует `PyUnicode_AsUTF8AndSize`, которого
   нет в limited API до 3.10; Python-слой и так требует ≥ 3.12. Один wheel покрывает 3.12+.
4. **Python-ядро без внешних зависимостей**: только stdlib и `ferronit._core`. Всё остальное —
   optional extras (`server`, `db`, `postgres`, `pydantic`).
5. **`ferronit._core.Response` — не тот же объект, что Python `Response`**: импортировать
   `from ferronit._core import Response as RustResp`.
6. **PyO3 `#[getter]` срезает префикс `get_`**: Rust `fn get_status()` → в Python `.status`.
   Тесты на заголовки нормализуют регистр (`Content-Encoding` с большой буквы).
7. **Python 3.14+**: сборка ядра только с `PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1`.
8. **Middleware не должен await'ить синхронные хендлеры**:
   `result = next_handler(req); if hasattr(result, "__await__"): result = await result`.
9. **`X-Trace-Id` ставить в `Ferronit.__call__` ПОСЛЕ `_to_response`** — иначе заголовок теряется
   на возвратах dict/pydantic.
10. **Что понимает `_to_response`**: `dict` и `list` (JSON через serde_json),
    кортеж `(payload, status)` — документированный способ задать статус
    (`return {"error": "not found"}, 404`), `Response`, `str` (text/plain),
    объект с `model_dump()` / dataclass (JSON), асинхронный итератор (стриминг).
    Для нестандартных случаев собирай `Response` явно.
11. **Публичное поведение меняется только вместе с тестом.** Сначала падающий тест, потом код.
12. **Новые публичные сущности — с докстрингом** (проверяется `scripts/agent_readiness.py`,
    порог 90% в `scripts/check.sh`).
13. **Окружение ведёт Poetry, wheel собирает maturin**: `poetry install --all-extras` +
    `poetry run maturin develop --release`. `poetry.lock` в репозитории — при правке
    зависимостей обновляй `poetry lock`, не удаляй файл. Проект Poetry не ставит
    (`package-mode = false`), потому что сборка нативная.

## Стиль кода

- **Типы обязательны** на публичных функциях и методах (аргументы + возврат). Пакет помечен
  `py.typed`, аннотации достоверны — на них опираются mypy и инструменты агента.
- **Докстринги — на английском, Google-style**, у каждой публичной сущности: первая строка —
  краткое описание, дальше `Args:` / `Returns:` / `Raises:` при необходимости.
  Комментарии внутри кода можно оставлять на русском, если так понятнее.
- **Публичный API объявляется в `__all__`** каждого модуля.
- Приватное — с префиксом `_`.
- Форматирование: ruff (line-length 100, target py312), сортировка импортов включена.
- Тесты: pytest, `asyncio_mode = "auto"` (маркер `@pytest.mark.asyncio` не нужен),
  файлы `tests/test_<тема>.py`, security — в `tests/security/`.

## Куда идти с задачей

| Задача | Файл |
|---|---|
| Роутинг, JSON, gzip, CORS на уровне ядра | `ferronit-rs/src/lib.rs` (Rust, нужна пересборка) |
| ASGI-движок, регистрация маршрутов, ошибки | `ferronit/core/app.py` |
| Заголовки/query/body запроса | `ferronit/core/request.py` |
| Ответы, стриминг | `ferronit/core/response.py` |
| Типы параметров в хендлерах | `ferronit/injection.py` |
| DDD / CQRS | `ferronit/ddd.py` |
| Порты и адаптеры | `ferronit/hexagonal.py`, `ferronit/contrib/*` |
| Доступ к БД | `ferronit/contrib/db.py` (CoreRepository — скорость, RelationalRepository — ORM-объекты), `ferronit/contrib/rawdb.py` + `rawmodel.py` (asyncpg без SQLAlchemy; модели сериализуются Rust-ядром напрямую); `ferronit.db` (запрос + JSON целиком в Rust, для больших выборок) |
| LLM / векторный поиск | `ferronit/contrib/llm.py`, `ferronit/contrib/vectordb.py` |
| Безопасность (заголовки, лимиты, rate limit) | `ferronit/contrib/security.py`, `ratelimit.py`, `docs/security/ASVS.md` |
| Scaffold новых проектов | `ferronit/cli.py` (шаблоны в начале файла) |

## Экономия контекста

- Вопросы «где что лежит / как связано» → сначала `graphify query "<вопрос>"` (готовый граф
  в `graphify-out/`), затем `graphify explain "<понятие>"`, и только потом grep.
- После правок кода: `graphify update .` (AST-only, без обращений к API).
- Карта архитектуры: `docs/architecture.html`. Требования безопасности: `docs/security/ASVS.md`.

## Границы работ

- Не добавлять внешние зависимости в `ferronit/` (только optional extras в `pyproject.toml`).
- Не трогать `bench/` (стенд замеров) при работе над фреймворком.
- Не коммитить: `.venv/`, `dist/`, `ferronit-rs/target/`, `*.session`, `.env`.
- Не переписывать числа в README без реального замера (`ab -n 30000 -c 50 -k`, Granian/uvicorn
  1 воркер, медиана прогонов).
- Не понижать abi3 и не менять фичи pyo3 «на месте» — только через `Cargo.toml` и пересборку.

## Известные ограничения

- **Хуков жизненного цикла нет**: `lifespan` только шлёт startup/shutdown.complete, `app.on_startup` /
  `app.on_shutdown` не существуют. Ресурсы (пул БД, прогрев кэша) открываются лениво или в
  `__main__` — см. `examples/rag_app.py` (`_seed_once`).
- **Логирования нет**: необработанное исключение → 500 без трейсбека (в `debug=True` исключение
  поднимается наружу). Для прода нужен свой access/error-лог.
- Rate limiter in-memory: при `--workers > 1` лимиты умножаются на число воркеров (для
  мультипроцессной работы нужен Redis-бэкенд).
- `EnvConfig.int/float/bool` при ошибке парсинга молча возвращают дефолт — «fail-fast» нет.
- CI нет (репозиторий без remote); перед коммитом запускай `./scripts/check.sh` локально.
