"""Релиз Ferronit: бамп версии во всех местах → check.sh → commit → tag → push.

Usage:
    python scripts/release.py 0.19.0 [--notes "…"] [--skip-check] [--no-push]

Перед запуском допиши записи в CHANGELOG.md (заголовок ``## <версия>``) — тогда
скрипт их не тронет. Иначе он вставит заголовок с датой (тело — из ``--notes``).
Версия живёт в 15 местах (см. ``TARGETS``); ``docs/vs-*.md`` и ``bench/go_*`` не трогаем.
Push тега ``v<версия>`` запускает CI-релиз (сборка колёс + PyPI + GitHub Release).
"""

from __future__ import annotations

import argparse
import datetime
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

TARGETS = [
    "ferronit/__init__.py",
    "ferronit-rs/Cargo.toml",
    "ferronit-rs/Cargo.lock",
    ".github/workflows/release.yml",
    "AGENTS.md",
    "Dockerfile",
    "README.md",
    "README.ru.md",
    "docker-compose.yml",
    "docker/demo_app.py",
    "docker/entrypoint.sh",
    "docs/getting-started.md",
    "llms.txt",
    "scripts/docker_smoke.sh",
]

# Здесь старая версия может остаться (исторические записи и сторонние крейты).
# poetry.lock — не наш номер: там constraints и хэши колес, которые могут содержать
# те же цифры (нижняя граница зависимости, sha256 колеса).
WHITELIST = ("CHANGELOG.md", "docs/vs-", "bench/go_", "poetry.lock")

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)


def git(*args: str) -> str:
    result = run(["git", *args])
    if result.returncode != 0:
        print(result.stderr.strip() or result.stdout.strip(), file=sys.stderr)
        sys.exit(1)
    return result.stdout


def current_version() -> str:
    text = (ROOT / "ferronit/__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not match:
        print("не нашёл __version__ в ferronit/__init__.py", file=sys.stderr)
        sys.exit(1)
    return match.group(1)


def bump_kind(old: str, new: str) -> str:
    o, n = tuple(map(int, old.split("."))), tuple(map(int, new.split(".")))
    if n[0] > o[0]:
        return "мажор"
    if n[1] > o[1]:
        return "минор"
    return "патч"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("new", help="новая версия, напр. 0.19.0")
    ap.add_argument("--notes", default=None, help="тело записи CHANGELOG (если заголовка ещё нет)")
    ap.add_argument("--skip-check", action="store_true", help="пропустить ./scripts/check.sh")
    ap.add_argument("--no-push", action="store_true", help="не пушить main и тег")
    args = ap.parse_args()

    new: str = args.new
    if not SEMVER.match(new):
        print(f"версия '{new}' не в формате x.y.z", file=sys.stderr)
        sys.exit(1)

    # рабочее дерево чистое, кроме CHANGELOG (туда пользователь пишет записи).
    dirty = git("status", "--porcelain").splitlines()
    unexpected = [d for d in dirty if d.split(None, 1)[1] != "CHANGELOG.md"]
    if unexpected:
        print("рабочее дерево не чистое (кроме CHANGELOG.md):", file=sys.stderr)
        for d in unexpected:
            print(" ", d, file=sys.stderr)
        sys.exit(1)

    old = current_version()
    if old == new:
        print(f"версия уже {new} — нечего делать")
        sys.exit(1)

    # 1) бамп версии по списку файлов.
    print(f"==> бамп {old} -> {new}")
    for rel in TARGETS:
        path = ROOT / rel
        text = path.read_text(encoding="utf-8")
        count = text.count(old)
        if count == 0:
            print(f"    [warn] {rel}: '{old}' не найдена — пропуск")
            continue
        path.write_text(text.replace(old, new), encoding="utf-8")
        print(f"    [ok] {rel}: {count}")

    # 2) старая версия не должна остаться вне whitelist.
    leftover = git("grep", "-n", old, "--", ".").splitlines()
    bad = [ln for ln in leftover if not any(ln.split(":", 1)[0].startswith(w) for w in WHITELIST)]
    if bad:
        print("старая версия осталась в неожиданных местах:", file=sys.stderr)
        for ln in bad:
            print(" ", ln, file=sys.stderr)
        sys.exit(1)

    # 3) CHANGELOG: вставить заголовок, если его нет.
    changelog = ROOT / "CHANGELOG.md"
    text = changelog.read_text(encoding="utf-8")
    if f"## {new}" not in text:
        date = datetime.date.today().isoformat()
        body = (args.notes or "").strip()
        block = f"## {new} — {date}\n\n{body}\n\n" if body else f"## {new} — {date}\n\n"
        marker = "`ferronit-rs/Cargo.toml`."
        idx = text.index(marker) + len(marker)
        changelog.write_text(text[:idx] + "\n" + block + text[idx:], encoding="utf-8")
        print(f"==> CHANGELOG: вставлен заголовок {new} (не забудь записи, если --notes пуст)")
    else:
        print(f"==> CHANGELOG: заголовок {new} уже есть")

    # 4) штатная проверка.
    if not args.skip_check:
        print("==> ./scripts/check.sh")
        result = run(["./scripts/check.sh"])
        if result.returncode != 0:
            tail = (result.stdout + result.stderr)[-3000:]
            print(tail, file=sys.stderr)
            sys.exit(1)

    # 5) commit + tag.
    kind = bump_kind(old, new)
    git("add", "-A")
    git("commit", "-m", f"chore: версия {new} ({kind}) во всех местах")
    git("tag", "-a", f"v{new}", "-m", f"Ferronit {new}")
    print(f"==> коммит и тег v{new} готовы")

    # 6) push (main + тег) — тег запускает CI-релиз.
    if not args.no_push:
        git("push", "origin", "main")
        git("push", "origin", f"v{new}")
        print(f"==> запушено: main + v{new} (CI-релиз запущен)")


if __name__ == "__main__":
    main()
