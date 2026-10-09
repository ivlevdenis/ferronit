# Ferrox

Высокопроизводительный Python ASGI-фреймворк с ядром на Rust. Маршрутизация,
разбор запроса, JSON, gzip и CORS работают в нативном коде; ваши хендлеры —
обычный Python.

**Документация:** [`docs/`](docs/README.md) — быстрый старт, роутинг, запросы,
инъекция, слой данных (три варианта), DDD/DI, Rust-ядро, бенчмарки, ASVS.

## Установка

```bash
pip install ferrox        # ставит сразу Python-слой и нативное ядро (ferrox._core + ferrox.db)
```

Ferrox — **один пакет**, собранный [maturin](https://www.maturin.rs)-ом как
mixed-проект. Ядро (`ferrox._core`) собирается с фичей `abi3-py312` — один wheel
работает на всех CPython от 3.12 до 3.14+. Внешних runtime-зависимостей нет.

```python
# app.py
from ferrox import Ferrox

app = Ferrox()

@app.route("/hello/{name}")
async def hello(name: str):
    return {"hello": name}
```

```bash
ferrox dev                          # dev-сервер (uvicorn, reload)
ferrox dev --server granian         # granian (Rust, ~4× быстрее)
ferrox run --workers 4              # прод: granian, fallback uvicorn
```

## Почему быстро

Маршрутизация — Rust `matchit`, фактически O(1): Ferrox не деградирует с ростом
числа маршрутов, FastAPI теряет до 4× внутри одной таблицы. Методика — только
`ab` (C-клиент); полный разбор — [`docs/benchmarks.md`](docs/benchmarks.md).

| Сценарий | Ferrox | FastAPI | Разрыв |
|---|---|---|---|
| uvicorn, 1000 маршрутов | 20 216 req/s | 2 817 req/s | ×7.2 |
| granian, 1000 маршрутов | 94 873 req/s | 3 416 req/s | **×27.8** |

## Слой данных

Три варианта под разные задачи — от «запрос → JSON целиком в Rust» до
полноценного ORM. Какой выбрать: [`docs/data/overview.md`](docs/data/overview.md).

## Разработка

```bash
poetry install --all-extras             # окружение в .venv + все зависимости
PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 poetry run maturin develop --release   # Rust-ядро
./scripts/check.sh                      # линт → типы → докстринги → сборка ядра → тесты
poetry run pytest tests/ -q             # 263 теста
```

Окружение ведёт Poetry (`poetry.toml` — venv внутри проекта), wheel с Rust-ядром собирает
maturin (см. `[tool.maturin]` в `pyproject.toml`), поэтому сам проект Poetry не ставит.

- **AGENTS.md** — инструкция для код-агентов (Claude Code, Codex, Cursor, ...).
- **`examples/`** — minimal, DI, LLM + SSE, RAG, WebSocket; `examples/app.py` — DDD-пример.
- Пакет помечен `py.typed`; для Rust-ядра лежит `ferrox/_core.pyi`.
- `bench/` — измерительный стенд (все цифры выше), не входит в пакет.

## Релиз

```bash
# версия правится в двух файлах: ferrox/__init__.py и ferrox-rs/Cargo.toml
git tag -a v0.8.2 -m "Ferrox 0.8.2" && git push origin v0.8.2
```

Тег `v*` запускает `.github/workflows/release.yml`: проверки и тесты → колёса (Linux glibc/musl,
macOS, Windows) и sdist → GitHub Release с артефактами → публикация на PyPI (Trusted Publishing).
Если версия тега не совпадает с `Cargo.toml`/`__init__.py`, workflow падает до публикации.

## Docker

```bash
docker build -t ferrox:0.8.1 .
docker run --rm -p 8000:8000 ferrox:0.8.1
```

## Лицензия

Apache License 2.0 — см. [LICENSE](LICENSE). Использование, изменение и распространение
разрешены, включая коммерческое; сохраняются уведомления об авторстве и патентная оговорка.

