# Ferronit — документация

Ferronit — быстрая замена легаси Python-сервисов, которые упираются в производительность.
Горячий путь (роутинг, JSON, gzip, CORS, PostgreSQL) — в Rust-ядре `ferronit._core`,
хендлеры — обычный Python.

Документация на русском. Карта:

## Начало работы
- [Быстрый старт](getting-started.md) — установка, первое приложение, CLI, Docker, структура проекта

## Основы
- [Маршрутизация и хендлеры](routing.md) — роуты, path-параметры, возвращаемые значения, ответы, ошибки
- [Запросы](requests.md) — `Request`: query, заголовки, cookies, тело, JSON, формы, файлы
- [Внедрение зависимостей](injection.md) — типизированные параметры, `Header`, body-модели
- [Ответы и сериализация](responses.md) — `Response`/`JSONResponse`/стриминг, модели, кодеки
- [Middleware и contrib](middleware.md) — CORS, security-заголовки, rate limiting, трассировка, health, статика
- [WebSocket](websocket.md) — текстовые, бинарные и JSON-сообщения, Origin-защита
- [OpenAPI](openapi.md) — автоматическая генерация схемы

## Слой данных
- [Какой слой выбрать](data/overview.md) — три слоя и таблица «когда что»
- [Запросы в Rust](data/rust-query.md) — `ferronit.db`: запрос → JSON целиком в Rust
- [Сырой asyncpg + модели](data/rawdb.md) — `RawUnitOfWork`, `Model`, query DSL
- [SQLAlchemy](data/sqlalchemy.md) — ORM и Core-репозитории

## Архитектура
- [DDD, DI и гексагональная](architecture.md) — порты/адаптеры, шины, контейнер, конфиг, CLI
- [Rust-ядро](rust-core.md) — что лежит в `ferronit._core` и зачем

## Справочно
- [Бенчмарки](benchmarks.md) — измеренные цифры и методика
- [ASVS Level 1](security/ASVS.md) — карта соответствия OWASP ASVS 5.0
