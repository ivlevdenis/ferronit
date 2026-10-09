#!/usr/bin/env bash
# Сборка релизных артефактов Ferrox в dist/
#
#   dist/ferrox-<ver>-cp312-abi3-<platform>.whl  — Python + ferrox_core + ferrox_core.db
#   dist/ferrox-<ver>.tar.gz                     — sdist (исходники, для релиза)
#
# Один wheel: Python-пакет (`ferrox/` + слой данных `ferrox.db`) и нативное ядро
# (maturin mixed проект, manifest-path в ferrox-rs/Cargo.toml) собираются вместе.
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

echo "==> 1/2 wheel (maturin, abi3-py312) — Python + ferrox_core + ferrox_core.db"
("$ROOT/.venv/bin/maturin" build --release --out "$DIST" -i "$PY")

echo "==> 2/2 sdist (maturin) — исходники с LICENSE и Cargo.lock"
("$ROOT/.venv/bin/maturin" sdist --out "$DIST")

echo "==> готово:"
ls -1 "$DIST"

echo
echo "Проверка установки в чистый venv:"
echo "  python3 -m venv /tmp/ferrox-check"
echo "  /tmp/ferrox-check/bin/pip install --no-index --find-links $DIST ferrox"
