"""CLI `ferronit new|dev|run`: каркас проекта, выбор сервера, поиск приложения.

Проверяем по факту: сгенерированный проект импортируется и отвечает по HTTP,
команды `dev`/`run` собирают правильную команду запуска (subprocess подменён),
а неверные условия дают внятную ошибку и код возврата.
"""

from __future__ import annotations

import builtins
import importlib
import sys

import pytest
from httpx import ASGITransport, AsyncClient

from ferronit import Ferronit, cli


def _main(monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    """Вызвать `cli.main()` с заданными аргументами."""
    monkeypatch.setattr(sys, "argv", ["ferronit", *argv])
    cli.main()


def _capture_runs(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Подменить `subprocess.run` и вернуть список собранных команд."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], check: bool = False) -> None:
        calls.append(cmd)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    return calls


# ── ferronit new ────────────────────────────────────────────────────────


def test_new_scaffolds_minimal_project_that_serves(tmp_path, monkeypatch, capsys) -> None:
    """Без --ddd: app.py + main.py + pyproject + README, и приложение отвечает."""
    monkeypatch.chdir(tmp_path)
    _main(monkeypatch, "new", "demo")

    files = sorted(p.name for p in tmp_path.joinpath("demo").iterdir())
    assert files == ["README.md", "app.py", "main.py", "pyproject.toml"]
    project = tmp_path / "demo"
    assert "minimal" in (project / "README.md").read_text(encoding="utf-8")
    assert "# demo" in (project / "README.md").read_text(encoding="utf-8")
    assert "demo" in (project / "app.py").read_text(encoding="utf-8")
    out = capsys.readouterr().out
    assert "Created demo/" in out and "cd demo" in out

    # Сгенерированный проект действительно поднимается как ASGI-приложение.
    sys.path.insert(0, str(project))
    try:
        module = importlib.import_module("app")
        assert isinstance(module.app, Ferronit)

        async def call() -> dict:
            transport = ASGITransport(app=module.app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/")
                return response.json()

        import asyncio

        assert asyncio.run(call()) == {"service": "demo", "ok": True}
    finally:
        sys.path.remove(str(project))
        sys.modules.pop("app", None)


def test_new_ddd_scaffolds_layers_and_valid_syntax(tmp_path, monkeypatch, capsys) -> None:
    """С --ddd: слои, пример агрегата, подставленное имя — и синтаксис всех файлов."""
    monkeypatch.chdir(tmp_path)
    _main(monkeypatch, "new", "shop", "--ddd")

    root = tmp_path / "shop"
    for layer in ("domain", "application", "infrastructure", "interfaces"):
        assert (root / layer / "__init__.py").exists()
    assert "DDD" in (root / "README.md").read_text(encoding="utf-8")
    assert "class Order(AggregateRoot)" in (root / "domain" / "orders.py").read_text(encoding="utf-8")

    generated = sorted(root.rglob("*.py"))
    assert len(generated) == 10
    # 4 × __init__.py слоёв + config + orders + services + db + api + main
    for path in generated:
        source = path.read_text(encoding="utf-8")
        compile(source, str(path), "exec")  # синтаксис шаблона
        assert "{name}" not in source, f"не подставлено имя проекта: {path}"
        assert "{{" not in source, f"остался экранированный шаблон: {path}"

    api = (root / "interfaces" / "api.py").read_text(encoding="utf-8")
    assert '"app": "shop"' in api


def test_new_refuses_existing_directory(tmp_path, monkeypatch, capsys) -> None:
    """Существующий каталог — ошибка с кодом возврата, а не перезапись."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "demo").mkdir()

    with pytest.raises(SystemExit) as exc:
        _main(monkeypatch, "new", "demo")

    assert exc.value.code == 1
    assert "already exists" in capsys.readouterr().err


# ── поиск приложения ───────────────────────────────────────────────────


def test_find_app_prefers_package_style_module(tmp_path, monkeypatch) -> None:
    """Сначала `имя_каталога/app.py` (пакетный запуск), иначе просто `app.py`, иначе None."""
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.chdir(project)
    assert cli._find_app() is None

    (project / "app.py").write_text("app = None\n", encoding="utf-8")
    assert cli._find_app() == "app:app"

    # Пакетный вариант приоритетнее, когда появился.
    (project / "proj").mkdir()
    (project / "proj" / "app.py").write_text("app = None\n", encoding="utf-8")
    assert cli._find_app() == "proj.app:app"


# ── ferronit dev ───────────────────────────────────────────────────────


def test_dev_runs_uvicorn_with_reload(tmp_path, monkeypatch) -> None:
    """По умолчанию dev — uvicorn с авто-релоадом на выбранном порту."""
    (tmp_path / "app.py").write_text("app = None\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    calls = _capture_runs(monkeypatch)

    _main(monkeypatch, "dev", "--port", "8123", "--host", "0.0.0.0")

    cmd = calls[0]
    assert cmd[1:4] == ["-m", "uvicorn", "app:app"]
    assert cmd[cmd.index("--port") + 1] == "8123"
    assert cmd[cmd.index("--host") + 1] == "0.0.0.0"
    assert "--reload" in cmd


def test_dev_uses_granian_when_asked(tmp_path, monkeypatch) -> None:
    """`--server granian` — другая команда, тот же модуль приложения."""
    (tmp_path / "app.py").write_text("app = None\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    calls = _capture_runs(monkeypatch)

    _main(monkeypatch, "dev", "--server", "granian")

    cmd = calls[0]
    assert cmd[1:3] == ["-m", "granian"]
    assert cmd[cmd.index("--interface") + 1] == "asgi"
    assert cmd[-1] == "app:app"
    assert "--reload" in cmd


def test_dev_explains_missing_app(tmp_path, monkeypatch, capsys) -> None:
    """Нет app.py и не передан --app — подсказка и код возврата."""
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc:
        _main(monkeypatch, "dev")

    assert exc.value.code == 1
    assert "No app.py found" in capsys.readouterr().err


# ── ferronit run ───────────────────────────────────────────────────────


def test_run_uses_granian_with_workers(tmp_path, monkeypatch) -> None:
    """Прод по умолчанию — Granian с числом воркеров."""
    (tmp_path / "app.py").write_text("app = None\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    calls = _capture_runs(monkeypatch)

    _main(monkeypatch, "run", "--workers", "4", "--port", "9000")

    cmd = calls[0]
    assert cmd[1:3] == ["-m", "granian"]
    assert cmd[cmd.index("--workers") + 1] == "4"
    assert cmd[cmd.index("--port") + 1] == "9000"
    assert "--reload" not in cmd


def test_run_falls_back_to_uvicorn_without_granian(tmp_path, monkeypatch, capsys) -> None:
    """Granian не установлен — падаем на uvicorn и говорим об этом в stderr."""
    (tmp_path / "app.py").write_text("app = None\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_check_granian", lambda exit_on_missing=True: None)
    calls = _capture_runs(monkeypatch)

    _main(monkeypatch, "run")

    assert calls[0][1:3] == ["-m", "uvicorn"]
    assert "falling back to uvicorn" in capsys.readouterr().err


def test_check_granian_exits_when_module_missing(monkeypatch, capsys) -> None:
    """Отсутствие Granian: по умолчанию выход с подсказкой, с флагом — None."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "granian":
            raise ImportError("нет granian")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    assert cli._check_granian(exit_on_missing=False) is None
    with pytest.raises(SystemExit) as exc:
        cli._check_granian()
    assert exc.value.code == 1
    assert "Granian not installed" in capsys.readouterr().err


# ── общее ──────────────────────────────────────────────────────────────


def test_no_subcommand_prints_help(monkeypatch, capsys) -> None:
    """Без подкоманды — справка и код 0, а не трейсбек."""
    with pytest.raises(SystemExit) as exc:
        _main(monkeypatch)

    assert exc.value.code == 0
    assert "usage: ferronit" in capsys.readouterr().out


def test_distribution_exposes_cli_entry_point() -> None:
    """`ferronit` в PATH — это точка входа `ferronit.cli:main` из метаданных."""
    from importlib import metadata as metadata

    scripts = {ep.name: ep.value for ep in metadata.entry_points(group="console_scripts")}
    assert scripts.get("ferronit") == "ferronit.cli:main"
