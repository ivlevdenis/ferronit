@AGENTS.md

Канон-инструкция для агентов лежит в `AGENTS.md` (импортируется выше).
Ниже — минимум, который нужен до его прочтения:

- Ferrox = единый пакет: Python `ferrox/` + одно Rust-ядро `ferrox-rs/` (→ `ferrox._core`, слой данных `ferrox.db`), Python ≥ 3.12.
- Правки в Rust требуют пересборки: `PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 poetry run maturin develop --release` (из корня).
- Проверка перед коммитом: `./scripts/check.sh` (ruff → mypy → сборка ядра → pytest).
- Типы обязательны, докстринги на английском (Google-style), публичный API — в `__all__`.
- Версию править в двух местах: `ferrox/__init__.py` и `ferrox-rs/Cargo.toml`.
