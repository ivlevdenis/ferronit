#!/usr/bin/env python3
"""Замер «читаемости пакета для код-агентов» и список недостающих докстрингов.

Зачем: агенту нужны докстринги (что делает) и типы (что принимает), а не догадки.
Скрипт считает покрытие публичного API докстрингами и аннотациями.

    python3 scripts/agent_readiness.py                 # сводка по файлам
    python3 scripts/agent_readiness.py --missing       # что именно без докстринга
    python3 scripts/agent_readiness.py --min-coverage 90   # ненулевой код, если ниже
    python3 scripts/agent_readiness.py --json          # машинный формат
"""

from __future__ import annotations

import argparse
import ast
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PKG = ROOT / "ferrox"


def _is_public(name: str) -> bool:
    return not name.startswith("_")


def _typed(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    a = node.args
    args = [*a.args, *a.posonlyargs, *a.kwonlyargs]
    return bool(node.returns) and all(
        arg.annotation is not None for arg in args if arg.arg not in ("self", "cls")
    )


def scan(path: pathlib.Path) -> dict:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    public: list[tuple[str, str, int, bool]] = []  # (kind, name, line, has_doc)
    total_fn = typed_fn = 0
    has_all = False

    def walk(body: list[ast.stmt], prefix: str = "") -> None:
        for node in body:
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == "__all__":
                        nonlocal has_all
                        has_all = True
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                nonlocal total_fn, typed_fn
                total_fn += 1
                typed_fn += 1 if _typed(node) else 0
                if _is_public(node.name) and not prefix:
                    public.append(("function", node.name, node.lineno,
                                   ast.get_docstring(node) is not None))
            elif isinstance(node, ast.ClassDef):
                if _is_public(node.name):
                    public.append(("class", node.name, node.lineno,
                                   ast.get_docstring(node) is not None))
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_public(sub.name):
                        public.append((f"method of {node.name}", sub.name, sub.lineno,
                                       ast.get_docstring(sub) is not None))
                    elif isinstance(sub, ast.ClassDef):
                        walk([sub], prefix or node.name)

    walk(tree.body)
    documented = sum(1 for _, _, _, has in public if has)
    return {
        "path": str(path.relative_to(ROOT)),
        "loc": len(path.read_text(encoding="utf-8").splitlines()),
        "public": len(public),
        "documented": documented,
        "all": has_all,
        "fn": total_fn,
        "typed": typed_fn,
        "missing": [(kind, name, line) for kind, name, line, has in public if not has],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--missing", action="store_true", help="показать сущности без докстринга")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--min-coverage", type=float, default=0.0)
    a = ap.parse_args()

    rows = [scan(p) for p in sorted(PKG.rglob("*.py"))]
    pub = sum(r["public"] for r in rows)
    doc = sum(r["documented"] for r in rows)
    fn = sum(r["fn"] for r in rows)
    typed = sum(r["typed"] for r in rows)
    coverage = (doc / pub * 100) if pub else 100.0

    if a.json:
        print(json.dumps({"coverage": round(coverage, 1), "files": rows}, ensure_ascii=False, indent=2))
    elif a.missing:
        for r in rows:
            if not r["missing"]:
                continue
            print(f"\n{r['path']}  ({len(r['missing'])} без докстринга)")
            for kind, name, line in r["missing"]:
                print(f"  {line:>4}: {kind} {name}")
    else:
        print(f"{'файл':<42}{'loc':>5}{'публичных':>11}{'с докстр.':>11}{'%':>7}  типы")
        for r in rows:
            pct = (r["documented"] / r["public"] * 100) if r["public"] else 100.0
            t = f"{r['typed']}/{r['fn']}" if r["fn"] else "—"
            flag = "" if pct >= 90 or not r["public"] else "  ←"
            print(f"{r['path']:<42}{r['loc']:>5}{r['public']:>11}{r['documented']:>11}{pct:>6.0f}%  {t}{flag}")
        print(f"\nИТОГО: докстринги {doc}/{pub} ({coverage:.0f}%), "
              f"полные типы {typed}/{fn} ({(typed / fn * 100) if fn else 100:.0f}%), "
              f"модулей без __all__: {sum(1 for r in rows if not r['all'])}")

    if coverage < a.min_coverage:
        print(f"\nНИЖЕ ПОРОГА: {coverage:.0f}% < {a.min_coverage:.0f}%", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
