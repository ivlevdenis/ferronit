"""Сборка и версии: единый пакет ferrox содержит Python-слой и нативное ядро.

`pip install ferrox` ставит всё сразу: пакет `ferrox/` (включая `ferrox.db` — слой
данных) и нативное ядро `ferrox._core`. Отдельных дистрибутивов `ferrox-core`/`ferrox-db`
быть не должно.
"""

from __future__ import annotations

import pathlib
import re
from importlib import metadata

import ferrox

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _cargo_version() -> str:
    text = (ROOT / "ferrox-rs" / "Cargo.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match, "в ferrox-rs/Cargo.toml нет version"
    return match.group(1)


def test_native_core_and_db_live_under_ferrox() -> None:
    """Ядро доступно как `ferrox._core`, слой данных — как `ferrox.db`."""
    import ferrox._core as core

    for name in ("Router", "Request", "Response", "FerroxApp"):
        assert hasattr(core, name), f"ferrox._core.{name} отсутствует"
    assert hasattr(core, "db"), "ferrox._core.db отсутствует"

    assert hasattr(ferrox, "db"), "ferrox.db отсутствует"
    for name in ("connect", "query_json"):
        assert hasattr(ferrox.db, name), f"ferrox.db.{name} отсутствует"
    assert ferrox.db.connect is core.db.connect
    assert ferrox.db.query_json is core.db.query_json


def test_single_distribution() -> None:
    """ferrox — один дистрибутив; зависимостей на ferrox-core/ferrox-db нет."""
    requires = metadata.requires("ferrox") or []
    for prefix in ("ferrox-core", "ferrox-db"):
        assert not any(d.replace(" ", "").startswith(prefix) for d in requires), (
            f"ferrox не должен зависеть от {prefix}: {requires}"
        )


def test_version_matches_cargo() -> None:
    """__version__ ferrox == version в ferrox-rs/Cargo.toml == версия дистрибутива."""
    assert ferrox.__version__ == _cargo_version(), (
        f"ferrox.__version__={ferrox.__version__} != Cargo.toml={_cargo_version()}"
    )
    assert metadata.version("ferrox") == ferrox.__version__, (
        f"ferrox {metadata.version('ferrox')} != {ferrox.__version__}"
    )


def test_core_is_abi3() -> None:
    """Ядро собрано с abi3 — один wheel на все CPython 3.12+."""
    text = (ROOT / "ferrox-rs" / "Cargo.toml").read_text(encoding="utf-8")
    assert "abi3-py" in text, "в ferrox-rs/Cargo.toml нет фичи abi3-pyXX"


def test_wheel_metadata_points_to_single_crate() -> None:
    """maturin mixed-проект: один manifest-path и один Python-пакет."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'manifest-path = "ferrox-rs/Cargo.toml"' in text
    assert 'module-name = "ferrox._core"' in text
    assert 'python-packages = ["ferrox"]' in text
