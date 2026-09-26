"""Сборка и версии: Rust-ядро должно ставиться вместе с velox и совпадать по версии.

Эти тесты ловят ровно тот баг, из-за которого `pip install velox` давал
неработающий пакет: Python-wheel собирался без .so и без зависимости на ядро.
"""

from __future__ import annotations

import pathlib
import re
from importlib import metadata

import pytest

import velox

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _cargo_version() -> str:
    text = (ROOT / "velox-rs" / "Cargo.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match, "в velox-rs/Cargo.toml нет version"
    return match.group(1)


def test_rust_core_importable() -> None:
    """velox_core импортируется и отдаёт все четыре класса."""
    import velox_core

    for name in ("Router", "Request", "Response", "VeloxApp"):
        assert hasattr(velox_core, name), f"velox_core.{name} отсутствует"


def test_velox_declares_core_dependency() -> None:
    """velox обязан тянуть velox-core — иначе wheel снова будет пустым."""
    deps = metadata.requires("velox") or []
    assert any(
        d.replace(" ", "").startswith("velox-core") for d in deps
    ), f"в метаданных velox нет зависимости velox-core: {deps}"


def test_versions_match() -> None:
    """__version__ velox == version в Cargo.toml == версия установленного ядра."""
    assert velox.__version__ == _cargo_version(), (
        f"velox.__version__={velox.__version__} != Cargo.toml={_cargo_version()}"
    )
    try:
        core_version = metadata.version("velox-core")
    except metadata.PackageNotFoundError:  # pragma: no cover
        pytest.skip("velox-core не установлен как дистрибутив (maturin develop)")
    assert core_version == velox.__version__, (
        f"ядро {core_version} != velox {velox.__version__}"
    )


def test_core_is_abi3() -> None:
    """Ядро собрано с abi3 — иначе придётся публиковать wheel под каждую версию Python."""
    text = (ROOT / "velox-rs" / "Cargo.toml").read_text(encoding="utf-8")
    assert "abi3-py" in text, "в Cargo.toml нет фичи abi3-pyXX"


def test_wheel_metadata_mentions_python_package() -> None:
    """hatchling должен собирать пакет velox явно, а не угадывать."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'packages = ["velox"]' in text
    assert 'path = "velox/__init__.py"' in text
