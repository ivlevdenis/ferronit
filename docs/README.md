# Ferronit — документация

Ferronit — быстрая замена легаси Python-сервисов, которые упираются в производительность.
Горячий путь (роутинг, JSON, gzip, CORS) — в Rust-ядре `ferronit._core`,
хендлеры — обычный Python.

Документация на русском. Карта:

## Начало работы
- [Быстрый старт](getting-started.md) — установка, первое приложение, CLI, Docker, структура проекта

## Основы
- [Маршрутизация и хендлеры](routing.md) — роуты, path-параметры, возвращаемые значения, ответы, ошибки
- [Запросы](requests.md) — `Request`: query, заголовки, cookies, тело, JSON, формы, файлы
- [Привязка параметров хендлера](binding.md) — типизированные параметры, `Header`, query/body-модели (`msgspec.Struct`), DI из контейнера
- [Ошибки и валидация](errors.md) — код на каждую ошибку, `errors` с ошибками полей и не только
- [Ответы и сериализация](responses.md) — `Response`/`JSONResponse`/стриминг, модели, кодеки
- [Middleware и contrib](middleware.md) — CORS, security-заголовки, rate limiting, трассировка, health, статика
- [WebSocket](websocket.md) — текстовые, бинарные и JSON-сообщения, Origin-защита
- [OpenAPI](openapi.md) — автоматическая генерация схемы

## Слой данных
- [Слой данных](data/overview.md) — rawdb + rawmodel: `RawUnitOfWork`, `Model`, `Relation`, query DSL

## Архитектура
- [DDD, DI и гексагональная](architecture.md) — порты/адаптеры, шины, контейнер, конфиг, CLI
- [Rust-ядро](rust-core.md) — что лежит в `ferronit._core` и зачем

## Справочно
- [Бенчмарки](benchmarks.md) — измеренные цифры и методика
- [Ferronit и Django](vs-django.md) — один сервис в трёх реализациях: код, что даёт Django из коробки, цена в строках и req/s
- [Ferronit и FastAPI](vs-fastapi.md) — тот же сервис на FastAPI (pydantic vs стенд): валидация, схема, цена в строках и req/s
- [Ferronit и BlackSheep](vs-blacksheep.md) — ближайший конкурент: DI и bindings, паритет на ping и разница на строках
- [Ferronit и Litestar](vs-litestar.md) — msgspec-модели и DI у обоих: разрыв ×2.4 на ping и паритет на чтениях
- [Ferronit и Tornado](vs-tornado.md) — не ASGI, свой pure-Python сервер: ×10 на ping (в основном сервер), ×1.4–2 на строках
- [Ferronit и Twisted](vs-twisted.md) — reactor + asyncio-мост: тот же вывод про сервер и слой данных
- [ASVS Level 1](security/ASVS.md) — карта соответствия OWASP ASVS 5.0
