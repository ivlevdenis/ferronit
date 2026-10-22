"""Сборка и версии: единый пакет ferronit содержит Python-слой и нативное ядро.

`pip install ferronit` ставит всё сразу: пакет `ferronit/` и нативное ядро
`ferronit._core`. Отдельных дистрибутивов `ferronit-core`/`ferronit-db` быть не должно.
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
    assert match, "в ferronit-rs/Cargo.toml нет version"
    return match.group(1)


def test_native_core_lives_under_ferronit() -> None:
    """Ядро доступно как `ferronit._core`."""
    import ferronit._core as core

    for name in ("Router", "Request", "Response", "FerronitApp"):
        assert hasattr(core, name), f"ferronit._core.{name} отсутствует"


def test_single_distribution() -> None:
    """ferronit — один дистрибутив; зависимостей на ferronit-core/ferronit-db нет."""
    requires = metadata.requires("ferronit") or []
    for prefix in ("ferronit-core", "ferronit-db"):
        assert not any(d.replace(" ", "").startswith(prefix) for d in requires), (
            f"ferronit не должен зависеть от {prefix}: {requires}"
        )


def test_msgspec_is_runtime_dependency() -> None:
    """msgspec — обязательная runtime-зависимость (не optional extra)."""
    requires = metadata.requires("ferronit") or []
    assert any("msgspec" in d.replace(" ", "") for d in requires), (
        f"msgspec должен быть обязательной зависимостью: {requires}"
    )


def test_asyncpg_is_optional_postgres_extra() -> None:
    """asyncpg — необязательный драйвер: только в extra «postgres», не в ядре."""
    requires = metadata.requires("ferronit") or []
    core = [d for d in requires if ";" not in d]
    assert not any("asyncpg" in d.replace(" ", "") for d in core), (
        f"asyncpg не должен быть обязательной зависимостью: {core}"
    )
    assert any(
        "asyncpg" in d.replace(" ", "")
        and 'extra=="postgres"' in d.replace(" ", "").replace("'", '"')
        for d in requires
    ), f"asyncpg должен быть в extra postgres: {requires}"


def _run_without_asyncpg(code: str) -> subprocess.CompletedProcess[str]:
    """Запустить код в отдельном процессе, где импорт asyncpg запрещён."""
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, timeout=120
    )


def test_data_layer_imports_without_asyncpg() -> None:
    """Без extra «postgres» слой данных импортируется — драйвер загружается лениво."""
    result = _run_without_asyncpg(
        "import sys; sys.modules['asyncpg'] = None\n"
        "import ferronit.contrib.drivers, ferronit.contrib.rawdb, ferronit.contrib.rawmodel\n"
        "print('ok')\n"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_postgres_pool_without_driver_explains_extra() -> None:
    """Без asyncpg create_pool для postgresql:// говорит, какой extra поставить."""
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
    """__version__ ferronit == version в ferronit-rs/Cargo.toml == версия дистрибутива."""
    assert ferronit.__version__ == _cargo_version(), (
        f"ferronit.__version__={ferronit.__version__} != Cargo.toml={_cargo_version()}"
    )
    assert metadata.version("ferronit") == ferronit.__version__, (
        f"ferronit {metadata.version('ferronit')} != {ferronit.__version__}"
    )


def test_core_is_abi3() -> None:
    """Ядро собрано с abi3 — один wheel на все CPython 3.12+."""
    text = (ROOT / "ferronit-rs" / "Cargo.toml").read_text(encoding="utf-8")
    assert "abi3-py" in text, "в ferronit-rs/Cargo.toml нет фичи abi3-pyXX"


def test_wheel_metadata_points_to_single_crate() -> None:
    """maturin mixed-проект: один manifest-path и один Python-пакет."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'manifest-path = "ferronit-rs/Cargo.toml"' in text
    assert 'module-name = "ferronit._core"' in text
    assert 'python-packages = ["ferronit"]' in text
