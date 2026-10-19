"""Микрозамер: цена сборки SQL на вызов (``sql.compile_statement``).

Гипотеза, которую он проверяет: на чтении ``list_100`` dict-слой
(``RawRepository.list``) отстаёт от модель-слоя (``RawModelRepository.list``) не
из-за разбора строк, а потому что первый компилирует оператор на каждый вызов, а
второй без фильтров берёт предкомпилированную строку (``self._list_sql``).
Гипотеза верна, если цена компиляции ≈ разнице между слоями по замеру
``bench/queries_vs_sqlalchemy.py``.

Запуск: .venv/bin/python bench/micro_bench_sql_compile.py
"""

from __future__ import annotations

import time

from ferronit.contrib import sql

N = 50000


def main() -> None:
    """Замерить компиляцию оператора ``list`` (тот же, что строит RawRepository)."""
    stmt = sql.Select("bench_rows", ("*",), where=[], limit=100, offset=0)

    start = time.perf_counter()
    for _ in range(N):
        sql.compile_statement(stmt, sql.POSTGRES)
    per_call_us = (time.perf_counter() - start) / N * 1e6

    print(f"compile_statement (list, 100 строк): {per_call_us:.1f} мкс/вызов, N={N}")
    print(f"то же в запросах/с (если это чистая потеря): {1e6 / per_call_us:,.0f}")


if __name__ == "__main__":
    main()
