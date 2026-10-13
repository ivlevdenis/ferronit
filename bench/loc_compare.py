"""Сравнение объёма одного и того же сервиса: Django-стенд против Ferronit.

Считает «значимые» строки (без пустых, комментариев и докстрингов) AST-обходом —
цифры для docs/vs-django.md.

    .venv/bin/python bench/loc_compare.py
"""

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Вариант C в git не хранится: api_bench.py при прогоне пишет его в
# bench/_bench_api_ferronit.py (см. .gitignore), поэтому источник — строка FERRONIT_APP.
# Так же берётся стендовая (без pydantic) версия FastAPI — FASTAPI_APP.
TARGETS: list[tuple[str, list[str] | tuple[str] | None]] = [
    ("A — Django 6.1 (ORM)", [
        "bench/django_bench/models.py", "bench/django_bench/views.py", "bench/django_bench/urls.py",
        "bench/django_bench/settings.py", "bench/django_bench/asgi.py",
    ]),
    ("B — Ferronit: агрегат + Repository", ["bench/django_bench/ferronit_service.py"]),
    ("C — Ferronit: + CQRS", None),
    ("F1 — FastAPI: pydantic + Depends + ORM", ["bench/fastapi_bench/app.py"]),
    ("F2 — FastAPI: без pydantic (стенд)", ("FASTAPI_APP",)),
    ("BS1 — BlackSheep: DI + bindings", ["bench/blacksheep_bench/app.py"]),
    ("BS2 — BlackSheep: asyncpg + msgspec (стенд)", ("BLACKSHEEP_APP",)),
    ("LS1 — Litestar: msgspec + DI + Parameter", ["bench/litestar_bench/app.py"]),
    ("LS2 — Litestar: asyncpg + msgspec (стенд)", ("LITESTAR_APP",)),
    ("T1 — Tornado: RequestHandler + asyncpg", ["bench/tornado_bench/app.py"]),
    ("TW1 — Twisted: Resource + Deferred.fromFuture", ["bench/twisted_bench/app.py"]),
]


def embedded_app(name: str) -> str:
    """Исходник приложения, вшитый строкой в bench/api_bench.py."""
    tree = ast.parse((ROOT / "bench/api_bench.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise SystemExit(f"{name} не найден в bench/api_bench.py")


def counts(src: str) -> tuple[int, int]:
    """(всего строк, значимых) для исходника."""
    tree = ast.parse(src)
    lines = src.splitlines()
    doc_lines: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and body:
            first = body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                doc_lines.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    code: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.stmt):
            code.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    noise = {i for i, line in enumerate(lines, 1)
             if not line.strip() or line.strip().startswith("#")}
    return len(lines), len(code - doc_lines - noise)


def main() -> None:
    for label, source in TARGETS:
        if source is None or isinstance(source, tuple):
            name = "FERRONIT_APP" if source is None else source[0]
            src = embedded_app(name)
            total, sig = counts(src)
            print(f"    строковый литерал {name} в bench/api_bench.py: "
                  f"всего {total}, значимых {sig}")
            print(f"{label}: 1 файл(ов)*, всего {total}, значимых {sig}\n")
            continue
        total = sig = 0
        for rel in source:
            a, b = counts((ROOT / rel).read_text())
            total += a
            sig += b
            print(f"    {rel}: всего {a}, значимых {b}")
        print(f"{label}: {len(source)} файл(ов), всего {total}, значимых {sig}\n")


if __name__ == "__main__":
    main()
