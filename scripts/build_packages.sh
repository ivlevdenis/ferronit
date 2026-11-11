#!/usr/bin/env bash
# Build Ferronit release artifacts into dist/
#
#   dist/ferronit-<ver>-cp312-abi3-<platform>.whl  — Python + ferronit_core
#   dist/ferronit-<ver>.tar.gz                     — sdist (sources, for the release)
#
# A single wheel: the Python package (`ferronit/`) and the native core (maturin
# mixed project, manifest-path in ferronit-rs/Cargo.toml) are built together.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${PY:-$ROOT/.venv/bin/python}"
DIST="$ROOT/dist"
rm -rf "$DIST"
mkdir -p "$DIST"

# pyo3 0.24 does not know about the 3.14 interpreter yet — the flag allows the
# abi3 build without waiting for a pyo3 update (safe for abi3 builds).
export PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1

echo "==> 1/2 wheel (maturin, abi3-py312) — Python + ferronit_core"
("$ROOT/.venv/bin/maturin" build --release --out "$DIST" -i "$PY")

echo "==> 2/2 sdist (maturin) — sources with LICENSE and Cargo.lock"
("$ROOT/.venv/bin/maturin" sdist --out "$DIST")

echo "==> done:"
ls -1 "$DIST"

echo
echo "Check the install into a clean venv:"
echo "  python3 -m venv /tmp/ferronit-check"
echo "  /tmp/ferronit-check/bin/pip install --no-index --find-links $DIST ferronit"
