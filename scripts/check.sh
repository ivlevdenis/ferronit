#!/usr/bin/env bash
# Единая проверка перед коммитом: линт → типы → сборка Rust-ядра → тесты.
#
#   ./scripts/check.sh              # всё
#   SKIP_CORE=1 ./scripts/check.sh  # без пересборки ядра (быстро, для правок на Python)
#
# Возвращает ненулевой код при первой же неудаче — годится для CI и для агентов.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${PY:-$ROOT/.venv/bin/python}"
MATURIN="$ROOT/.venv/bin/maturin"

if [ ! -x "$PY" ]; then
    echo "нет интерпретатора $PY" >&2
    echo "создай окружение: uv venv .venv && uv pip install --python .venv/bin/python -e '.[dev]'" >&2
    exit 1
fi

step() { printf '\n==> %s\n' "$*"; }

step "ruff (линт)"
"$PY" -m ruff check ferrox tests

step "докстринги публичного API (порог 90%)"
"$PY" scripts/agent_readiness.py --min-coverage "${MIN_DOCS_COVERAGE:-90}"

step "mypy (типы)"
"$PY" -m mypy ferrox

if [ "${SKIP_CORE:-0}" = "1" ]; then
    step "Rust-ядро: пропущено (SKIP_CORE=1)"
elif [ -x "$MATURIN" ]; then
    step "Rust-ядро (maturin, abi3) — ferrox_core + ferrox_core.db"
    (PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 "$MATURIN" develop --release 2>&1 | tail -3)
else
    step "Rust-ядро: maturin не найден, пропускаю"
fi

step "pytest (тесты)"
"$PY" -m pytest tests/ -q

step "готово: линт, типы, ядро и тесты прошли"
