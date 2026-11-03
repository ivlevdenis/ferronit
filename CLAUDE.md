@AGENTS.md

Канон-инструкция для агентов лежит в `AGENTS.md` (импортируется выше).
Ниже — минимум, который нужен до его прочтения:

- Ferronit = единый пакет: Python `ferronit/` + одно Rust-ядро `ferronit-rs/` (→ `ferronit._core`), Python ≥ 3.12; слой данных — rawdb/rawmodel для PostgreSQL (asyncpg), MySQL (aiomysql), SQLite (aiosqlite), все три драйвера — необязательные extras.
- Правки в Rust требуют пересборки: `PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 poetry run maturin develop --release` (из корня).
- Проверка перед коммитом: `./scripts/check.sh` (ruff → mypy → сборка ядра → pytest).
- Типы обязательны, докстринги на английском (Google-style), публичный API — в `__all__`.
- Версию править в двух местах: `ferronit/__init__.py` и `ferronit-rs/Cargo.toml`.
- Бенчмарки: основной конкурент — BlackSheep, сравниваем с ним по умолчанию (детали в AGENTS.md).
- Бенчмарки запускать **только на производительных (P) ядрах**, если пользователь явно не попросил иначе:
  серверы — `BENCH_SERVER_CPUS=0-5`, клиент `ab` — `BENCH_CLIENT_CPUS=6-11`. E-ядра `12-19` не использовать.
  После запуска проверять фактическую affinity (`taskset -pc <pid>`), а в подписи диаграммы/ответе указывать,
  как раскладывались ядра.
- Доставка артефактов: изображения и диаграммы бенчмарков отправлять **только** в Mattermost-канал
  `ferronit-omp` (Ferronit-OMP, channel_id `kdsscsgqm7nu5mmte9m5ssop5r`). Не в `ferronit-pi`.
  Как отправлять: `POST $MATTERMOST_URL/api/v4/files` (multipart: `channel_id`, `files`) →
  `POST $MATTERMOST_URL/api/v4/posts` (`channel_id`, `message`, `file_ids`), заголовок
  `Authorization: Bearer $MATTERMOST_TOKEN`; бот должен быть участником канала.
