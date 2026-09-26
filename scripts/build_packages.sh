#!/usr/bin/env bash
# Сборка обоих дистрибутивов Velox в dist/
#
#   dist/velox_core-<ver>-cp312-abi3-<platform>.whl  — Rust-ядро (maturin)
#   dist/velox-<ver>-py3-none-any.whl                — Python-пакет (hatchling)
#
# Порядок важен: Python-пакет объявляет зависимость velox-core, поэтому
# ядро собирается первым и попадает в тот же каталог для --find-links.
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

echo "==> 1/2 Rust-ядро (maturin, abi3-py312)"
(cd velox-rs && "$ROOT/.venv/bin/maturin" build --release --out "$DIST" -i "$PY")

echo "==> 2/2 Python-пакет (hatchling)"
uv build --wheel --out-dir "$DIST" .

echo "==> готово:"
ls -1 "$DIST"/*.whl

echo
echo "Проверка установки в чистый venv:"
echo "  uv venv /tmp/velox-check -q"
echo "  uv pip install --python /tmp/velox-check/bin/python --no-index --find-links $DIST velox"
