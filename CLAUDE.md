@AGENTS.md

Канон-инструкция для агентов лежит в `AGENTS.md` (импортируется выше).
Ниже — минимум, который нужен до его прочтения:

- Ferronit = единый пакет: Python `ferronit/` + одно Rust-ядро `ferronit-rs/` (→ `ferronit._core`, слой данных `ferronit.db`), Python ≥ 3.12.
- Правки в Rust требуют пересборки: `PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 poetry run maturin develop --release` (из корня).
- Проверка перед коммитом: `./scripts/check.sh` (ruff → mypy → сборка ядра → pytest).
- Типы обязательны, докстринги на английском (Google-style), публичный API — в `__all__`.
- Версию править в двух местах: `ferronit/__init__.py` и `ferronit-rs/Cargo.toml`.
