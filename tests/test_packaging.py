"""Build and versions: the single ferronit package holds the Python layer and the native core.

`pip install ferronit` installs everything at once: the `ferronit/` package and the native
core `ferronit._core`. There must be no separate `ferronit-core`/`ferronit-db` distributions.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys
from importlib import metadata

import ferronit

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _cargo_version() -> str:
    text = (ROOT / "ferronit-rs" / "Cargo.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match, "ferronit-rs/Cargo.toml has no version"
    return match.group(1)


def test_native_core_lives_under_ferronit() -> None:
    """The core is available as `ferronit._core`."""
    import ferronit._core as core

    for name in ("Router", "Request", "FerronitApp", "build_set_cookie", "parse_headers", "first_header"):
        assert hasattr(core, name), f"ferronit._core.{name} is missing"


def test_single_distribution() -> None:
    """ferronit is a single distribution; there are no ferronit-core/ferronit-db deps."""
    requires = metadata.requires("ferronit") or []
    for prefix in ("ferronit-core", "ferronit-db"):
        assert not any(d.replace(" ", "").startswith(prefix) for d in requires), (
            f"ferronit must not depend on {prefix}: {requires}"
        )


def test_msgspec_is_runtime_dependency() -> None:
    """msgspec is a required runtime dependency (not an optional extra)."""
    requires = metadata.requires("ferronit") or []
    assert any("msgspec" in d.replace(" ", "") for d in requires), (
        f"msgspec must be a required dependency: {requires}"
    )


def test_asyncpg_is_optional_postgres_extra() -> None:
    """asyncpg is an optional driver: only in the "postgres" extra, not in the core."""
    requires = metadata.requires("ferronit") or []
    core = [d for d in requires if ";" not in d]
    assert not any("asyncpg" in d.replace(" ", "") for d in core), (
        f"asyncpg must not be a required dependency: {core}"
    )
    assert any(
        "asyncpg" in d.replace(" ", "")
        and 'extra=="postgres"' in d.replace(" ", "").replace("'", '"')
        for d in requires
    ), f"asyncpg must be in the postgres extra: {requires}"


def _run_without_asyncpg(code: str) -> subprocess.CompletedProcess[str]:
    """Run code in a separate process where importing asyncpg is forbidden."""
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, timeout=120
    )


def test_data_layer_imports_without_asyncpg() -> None:
    """Without the "postgres" extra the data layer imports — the driver loads lazily."""
    result = _run_without_asyncpg(
        "import sys; sys.modules['asyncpg'] = None\n"
        "import ferronit.contrib.drivers, ferronit.contrib.rawdb, ferronit.contrib.rawmodel\n"
        "print('ok')\n"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_postgres_pool_without_driver_explains_extra() -> None:
    """Without asyncpg, create_pool for postgresql:// says which extra to install."""
    result = _run_without_asyncpg(
        "import asyncio, sys; sys.modules['asyncpg'] = None\n"
        "from ferronit.contrib.rawdb import create_pool\n"
        "try:\n"
        "    asyncio.run(create_pool('postgresql://user@host/db'))\n"
        "except ModuleNotFoundError as exc:\n"
        "    print(exc)\n"
        "else:\n"
        "    raise SystemExit('create_pool must fail without asyncpg')\n"
    )
    assert result.returncode == 0, result.stderr
    assert "ferronit[postgres]" in result.stdout


def test_version_matches_cargo() -> None:
    """__version__ of ferronit == version in ferronit-rs/Cargo.toml == distribution version."""
    assert ferronit.__version__ == _cargo_version(), (
        f"ferronit.__version__={ferronit.__version__} != Cargo.toml={_cargo_version()}"
    )
    assert metadata.version("ferronit") == ferronit.__version__, (
        f"ferronit {metadata.version('ferronit')} != {ferronit.__version__}"
    )


def test_core_is_abi3() -> None:
    """The core is built with abi3 — one wheel for all CPython 3.12+."""
    text = (ROOT / "ferronit-rs" / "Cargo.toml").read_text(encoding="utf-8")
    assert "abi3-py" in text, "ferronit-rs/Cargo.toml has no abi3-pyXX feature"


def test_wheel_metadata_points_to_single_crate() -> None:
    """maturin mixed project: a single manifest-path and a single Python package."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'manifest-path = "ferronit-rs/Cargo.toml"' in text
    assert 'module-name = "ferronit._core"' in text
    assert 'python-packages = ["ferronit"]' in text
