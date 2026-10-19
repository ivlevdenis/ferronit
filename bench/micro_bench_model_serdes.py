"""Микро-стенд модели: десериализация строки и сериализация ответа по компонентам.

Зачем: ``micro_bench_model.py`` показывает, сколько стоит маппинг строки в модель, но не
отвечает на вопрос «где именно теряется время» — в конструкторе ``msgspec``, в обходе
``asyncpg.Record``, в C-энкодере или в экранировании готового тела. Здесь оба конца
модельного пути разложены по вариантам, и рядом печатается CPU процесса
(``time.process_time``) — по нему видно, сколько работы реально съедает GIL, а не шум
планировщика.

Строки — настоящие ``asyncpg.Record`` из локального PostgreSQL, поэтому сравнение
честное: самый дешёвый вариант (``tuple``) не может быть медленнее самого обхода.

Запуск: .venv/bin/python bench/micro_bench_model_serdes.py
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import NamedTuple

import asyncpg
import msgspec

from ferronit._core import Response as RustResp
from ferronit.contrib.rawmodel import Model
from ferronit.core.response import _escape_html

DSN = os.environ.get(
    "FERRONIT_TEST_PG_DSN", "postgresql://postgres:postgres@localhost:5432/postgres"
)
REPEATS = 500
ENCODER = msgspec.json.Encoder()


class Wide(Model):
    """Широкая модель: десять колонок разных типов, как в реальной выборке."""

    __table__ = "serdes_wide"
    id: int = 0
    c1: str = ""
    c2: str = ""
    c3: int = 0
    c4: int = 0
    c5: bool = False
    c6: float = 0.0
    c7: str = ""
    c8: str = ""
    c9: str = ""


class WideArray(Model, array_like=True):
    """То же, но ``array_like``: позиционная сборка и кодирование без имён полей."""

    __table__ = "serdes_wide"
    id: int
    c1: str
    c2: str
    c3: int
    c4: int
    c5: bool
    c6: float
    c7: str
    c8: str
    c9: str


class WideNoGC(Model, gc=False):
    """То же, но без отслеживания сборщиком мусора (``gc=False``)."""

    __table__ = "serdes_wide"
    id: int = 0
    c1: str = ""
    c2: str = ""
    c3: int = 0
    c4: int = 0
    c5: bool = False
    c6: float = 0.0
    c7: str = ""
    c8: str = ""
    c9: str = ""


class StructRow(msgspec.Struct):
    """Строка из трёх полей обычным ``msgspec.Struct`` — точка отсчёта для других типов."""

    id: int = 0
    name: str = ""
    email: str = ""


@dataclass
class DC:
    """Та же строка dataclass'ом: msgspec её кодирует, но собирается она иначе."""

    id: int
    name: str
    email: str


class NT(NamedTuple):
    """Та же строка namedtuple'ом: msgspec кодирует её массивом, а не объектом."""

    id: int
    name: str
    email: str


def cpu_best(fn, repeats: int = REPEATS) -> tuple[float, float]:
    """Минимальные wall- и CPU-времена прогона ``fn`` из ``repeats`` попыток.

    Возвращает секунды на прогон. Минимум берётся отдельно по каждому счётчику:
    wall ловит ускорение кода, CPU — работу, за которую платит процесс.
    """
    fn()
    best = (float("inf"), float("inf"))
    for _ in range(repeats):
        w0, c0 = time.perf_counter(), time.process_time()
        fn()
        w, c = time.perf_counter() - w0, time.process_time() - c0
        best = (min(best[0], w), min(best[1], c))
    return best


def escape_fast(body: bytes) -> bytes:
    """Экранирование с предварительной проверкой: сканировать дешевле, чем заменять.

    Три ``bytes.replace`` всегда проходят всё тело целиком; типичное тело из БД этих
    байтов не содержит вовсе, поэтому сначала один C-скан на ``<``/``>``/``&``.
    """
    if b"<" in body or b">" in body or b"&" in body:
        return _escape_html(body)
    return body


def bench_row_types(records: list) -> dict[str, tuple[float, float]]:
    """Чем можно заменить ``msgspec.Struct``: что msgspec кодирует и чего это стоит.

    msgspec принимает только Struct, dataclass, namedtuple и контейнеры — произвольный
    класс с ``__struct_fields__`` он отвергает (``TypeError``). Здесь видно цену сборки
    каждого варианта и форму JSON, которую он даёт (по первым трём колонкам строки).
    """
    n = len(records)

    class SlotRow:
        """Обычный класс со слотами: то, что «между» Struct и строкой драйвера."""

        __slots__ = ("id", "name", "email")

        def __init__(self, row_id: int, name: str, email: str) -> None:
            self.id = row_id
            self.name = name
            self.email = email

    builders = {
        "Struct": lambda: [StructRow(*r[:3]) for r in records],
        "dataclass": lambda: [DC(*r[:3]) for r in records],
        "namedtuple": lambda: [NT(*r[:3]) for r in records],
        "класс со слотами": lambda: [SlotRow(*r[:3]) for r in records],
        "tuple": lambda: [tuple(r[:3]) for r in records],
    }
    out: dict[str, tuple[float, float]] = {}
    shapes: list[str] = []
    for label, build in builders.items():
        out[f"{label}: сборка"] = cpu_best(build)
        objects = build()
        try:
            body = msgspec.json.encode(objects)
        except TypeError:
            out[f"{label}: JSON"] = (float("inf"), float("inf"))
            shapes.append(f"{label} — не кодируется")
            continue
        out[f"{label}: JSON"] = cpu_best(lambda objs=objects: msgspec.json.encode(objs))
        shapes.append(f"{label} {body[:14].decode()}… ({len(body)} б)")
    print(f"  форма JSON на {n} строках: " + "; ".join(shapes))
    return out


def ms(seconds: float, n: int) -> str:
    """µs на элемент выборки из ``n`` штук."""
    return f"{seconds / n * 1e6:6.2f}"


def report(title: str, rows: dict[str, tuple[float, float]], n: int) -> None:
    """Печатает блок «вариант — wall µs — CPU µs» и отношение к самому дешёвому."""
    floor = min(w for w, _ in rows.values())
    print(f"\n{title} ({n} элементов, лучшее из {REPEATS}, µs на элемент)")
    for label, (w, c) in rows.items():
        print(f"  {label:30s} wall {ms(w, n)} | cpu {ms(c, n)} | ×{w / floor:4.2f}")


def bench_deser(records: list) -> dict[str, tuple[float, float]]:
    """Варианты превращения ``asyncpg.Record`` в модель."""
    return {
        "tuple(row) — потолок": cpu_best(lambda: [tuple(r) for r in records]),
        "dict(row) — для сравнения": cpu_best(lambda: [dict(r) for r in records]),
        "cls(*row) — текущий": cpu_best(lambda: [Wide(*r) for r in records]),
        "cls(*tuple(row))": cpu_best(lambda: [Wide(*tuple(r)) for r in records]),
        "cls.from_row(row)": cpu_best(lambda: [Wide.from_row(r) for r in records]),
        "array_like cls(*row)": cpu_best(lambda: [WideArray(*r) for r in records]),
        "gc=False cls(*row)": cpu_best(lambda: [WideNoGC(*r) for r in records]),
    }


def bench_ser(models: list, dicts: list) -> dict[str, tuple[float, float]]:
    """Варианты сериализации списка моделей в тело ответа."""
    payload = msgspec.json.encode(models)
    return {
        "msgspec.json.encode": cpu_best(lambda: msgspec.json.encode(models)),
        "Encoder(shared).encode": cpu_best(lambda: ENCODER.encode(models)),
        "+_escape_html — как в ядре": cpu_best(
            lambda: _escape_html(msgspec.json.encode(models))
        ),
        "_escape_html поверх тела": cpu_best(lambda: _escape_html(payload)),
        "escape по проверке": cpu_best(lambda: escape_fast(payload)),
        "array_like encode": cpu_best(
            lambda: msgspec.json.encode([WideArray(*msgspec.structs.astuple(m)) for m in models])
        ),
        "Rust Response.json(models)": cpu_best(lambda: RustResp.json(models, 200)),
        "Rust Response.json(dicts)": cpu_best(lambda: RustResp.json(dicts, 200)),
        "msgspec на dict(row)": cpu_best(lambda: msgspec.json.encode(dicts)),
        "json.dumps на dict(row)": cpu_best(lambda: json.dumps(dicts)),
    }


async def main() -> None:
    """Гоняет стенд на локальном PostgreSQL."""
    try:
        conn = await asyncpg.connect(DSN)
    except Exception as exc:  # pragma: no cover - зависит от окружения
        print(f"PostgreSQL недоступен ({type(exc).__name__}); подними контейнер на :5432")
        return

    try:
        await conn.execute(DDL_SQL)
        if await conn.fetchval("SELECT count(*) FROM serdes_wide") == 0:
            await conn.execute(SEED_SQL)
        for n in (1, 100, 1000):
            rows = await conn.fetch(f"SELECT * FROM serdes_wide LIMIT {n}")
            models = [Wide(*r) for r in rows]
            dicts = [dict(r) for r in rows]
            size_kb = len(msgspec.json.encode(models)) / 1024
            print(f"\n{'=' * 84}\nвыборка {n} строк, {size_kb:.1f} КБ JSON")
            report("десериализация: строка → модель", bench_deser(rows), n)
            report("сериализация: модели → тело", bench_ser(models, dicts), n)
            report("типы строк (первые 3 колонки)", bench_row_types(rows), n)
    finally:
        await conn.close()


DDL_SQL = """
CREATE TABLE IF NOT EXISTS serdes_wide (
    id serial PRIMARY KEY,
    c1 text, c2 text, c3 integer, c4 integer, c5 boolean,
    c6 double precision, c7 text, c8 text, c9 text
)
"""
SEED_SQL = (
    "INSERT INTO serdes_wide (c1, c2, c3, c4, c5, c6, c7, c8, c9) "
    "SELECT 'a' || g, 'b' || g, g, g * 2, g % 2 = 0, g * 1.5, "
    "'c' || g, 'd' || g, 'e' || g FROM generate_series(1, 1000) g"
)


if __name__ == "__main__":
    asyncio.run(main())
