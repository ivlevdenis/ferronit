#!/usr/bin/env bash
# Сборка единого пакета Ferrox в dist/
#
#   dist/ferrox-<ver>-cp312-abi3-<platform>.whl  — Python + ferrox_core + ferrox_core.db
#
# Один wheel: Python-пакет (`ferrox/`, `ferrox_db/`) и нативное ядро (maturin mixed
# проект, manifest-path в ferrox-rs/Cargo.toml) собираются вместе.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${PY:-$ROOT/.venv/bin/python}"
DIST="$ROOT/dist"
rm -rf "$DIST"
mkdir -p "$DIST"

# pyo3 0.24 ещё не знает про интерпретатор 3.14 — флаг разрешает сборку abi3,
# не дожидаясь обновления pyo3 (для abi3-сборок это безопасно).
export PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1

echo "==> 1/1 ferrox (maturin, abi3-py312) — Python + ferrox_core + ferrox_core.db"
("$ROOT/.venv/bin/maturin" build --release --out "$DIST" -i "$PY")

echo "==> готово:"
ls -1 "$DIST"/*.whl

echo
echo "Проверка установки в чистый venv:"
echo "  uv venv /tmp/ferrox-check -q"
echo "  uv pip install --python /tmp/ferrox-check/bin/python --no-index --find-links $DIST ferrox"
