#!/usr/bin/env bash
# Single pre-commit check: lint → types → Rust core build → tests.
#
#   ./scripts/check.sh              # everything
#   SKIP_CORE=1 ./scripts/check.sh  # skip the core rebuild (fast, for Python-only edits)
#
# Returns a non-zero code at the first failure — suitable for CI and for agents.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${PY:-$ROOT/.venv/bin/python}"
MATURIN="$ROOT/.venv/bin/maturin"

if [ ! -x "$PY" ]; then
    echo "no interpreter at $PY" >&2
    echo "create it: poetry install --all-extras (+ poetry run maturin develop --release)" >&2
    exit 1
fi

step() { printf '\n==> %s\n' "$*"; }

step "ruff (lint)"
"$PY" -m ruff check ferronit tests

step "public API docstrings (threshold 90%)"
"$PY" scripts/agent_readiness.py --min-coverage "${MIN_DOCS_COVERAGE:-90}"

step "mypy (types)"
"$PY" -m mypy ferronit

if [ "${SKIP_CORE:-0}" = "1" ]; then
    step "Rust core: skipped (SKIP_CORE=1)"
elif [ -x "$MATURIN" ]; then
    step "Rust core (maturin, abi3) — ferronit_core"
    (PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 "$MATURIN" develop --release 2>&1 | tail -3)
else
    step "Rust core: maturin not found, skipping"
fi

step "pytest (tests)"
"$PY" -m pytest tests/ -q

step "done: lint, types, core and tests passed"
