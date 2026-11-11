"""Ferronit release: bump the version everywhere → check.sh → commit → tag → push.

Usage:
    python scripts/release.py <version> [--notes "…"] [--skip-check] [--no-push]

Before running, add entries to CHANGELOG.md (heading ``## <version>``) — then the
script leaves them alone. Otherwise it inserts a dated heading (body from ``--notes``).
The version lives in 15 places (see ``TARGETS``); ``docs/vs-*.md`` is left untouched.
Pushing the ``v<version>`` tag triggers the CI release (wheel build + PyPI + GitHub Release).
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

# The old version may stay here (historical records and third-party crates).
# poetry.lock is not our number: it holds constraints and wheel hashes that may
# contain the same digits (a dependency lower bound, a wheel sha256).
WHITELIST = ("CHANGELOG.md", "docs/vs-", "poetry.lock")

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
        print("no __version__ found in ferronit/__init__.py", file=sys.stderr)
        sys.exit(1)
    return match.group(1)


def bump_kind(old: str, new: str) -> str:
    o, n = tuple(map(int, old.split("."))), tuple(map(int, new.split(".")))
    if n[0] > o[0]:
        return "major"
    if n[1] > o[1]:
        return "minor"
    return "patch"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("new", help="the new version in x.y.z format")
    ap.add_argument(
        "--notes", default=None, help="CHANGELOG entry body (if there is no heading yet)"
    )
    ap.add_argument("--skip-check", action="store_true", help="skip ./scripts/check.sh")
    ap.add_argument("--no-push", action="store_true", help="do not push main and the tag")
    args = ap.parse_args()

    new: str = args.new
    if not SEMVER.match(new):
        print(f"version '{new}' is not in x.y.z format", file=sys.stderr)
        sys.exit(1)

    # the working tree is clean except CHANGELOG (the user writes entries there).
    dirty = git("status", "--porcelain").splitlines()
    unexpected = [d for d in dirty if d.split(None, 1)[1] != "CHANGELOG.md"]
    if unexpected:
        print("the working tree is not clean (except CHANGELOG.md):", file=sys.stderr)
        for d in unexpected:
            print(" ", d, file=sys.stderr)
        sys.exit(1)

    old = current_version()
    if old == new:
        print(f"version is already {new} — nothing to do")
        sys.exit(1)

    # 1) bump the version across the file list.
    print(f"==> bump {old} -> {new}")
    for rel in TARGETS:
        path = ROOT / rel
        text = path.read_text(encoding="utf-8")
        count = text.count(old)
        if count == 0:
            print(f"    [warn] {rel}: '{old}' not found — skipping")
            continue
        path.write_text(text.replace(old, new), encoding="utf-8")
        print(f"    [ok] {rel}: {count}")

    # 2) the old version must not be left outside the whitelist.
    # -F: search for the number as a literal. Otherwise the dot in a version
    # number acts as "any character" and matches hex hashes in lock files — a false leftover.
    leftover = git("grep", "-n", "-F", old, "--", ".").splitlines()
    bad = [ln for ln in leftover if not any(ln.split(":", 1)[0].startswith(w) for w in WHITELIST)]
    if bad:
        print("the old version is still in unexpected places:", file=sys.stderr)
        for ln in bad:
            print(" ", ln, file=sys.stderr)
        sys.exit(1)

    # 3) CHANGELOG: insert the heading if it is not there yet.
    changelog = ROOT / "CHANGELOG.md"
    text = changelog.read_text(encoding="utf-8")
    if f"## {new}" not in text:
        date = datetime.date.today().isoformat()
        body = (args.notes or "").strip()
        block = f"## {new} — {date}\n\n{body}\n\n" if body else f"## {new} — {date}\n\n"
        marker = "`ferronit-rs/Cargo.toml`."
        idx = text.index(marker) + len(marker)
        changelog.write_text(text[:idx] + "\n" + block + text[idx:], encoding="utf-8")
        print(
            f"==> CHANGELOG: inserted the {new} heading "
            "(do not forget the entries if --notes is empty)"
        )
    else:
        print(f"==> CHANGELOG: the {new} heading is already there")

    # 4) the regular check.
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
    git("commit", "-m", f"chore: version {new} ({kind}) everywhere")
    git("tag", "-a", f"v{new}", "-m", f"Ferronit {new}")
    print(f"==> commit and tag v{new} are ready")

    # 6) push (main + tag) — the tag triggers the CI release.
    if not args.no_push:
        git("push", "origin", "main")
        git("push", "origin", f"v{new}")
        print(f"==> pushed: main + v{new} (CI release triggered)")


if __name__ == "__main__":
    main()
