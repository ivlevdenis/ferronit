"""Пробник: CPU на стороне PostgreSQL, а не только клиента.

Дополняет `queries_vs_sqlalchemy.py`: там CPU считался по процессу клиента
(`time.process_time()`), здесь — по cgroup контейнера с базой. Даёт цену запроса
глазами сервера: сколько микросекунд CPU PostgreSQL тратит на запрос.

Запуск: .venv/bin/python bench/_probe_pg_cpu.py
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from queries_vs_sqlalchemy import build_variants, seed  # noqa: E402

CONTAINER = "velox-pg"
CASES = (("get_1", 5000), ("list_100", 2000), ("insert_1", 2000))


def cgroup_cpu_us() -> int:
    """CPU-время контейнера с базой, микросекунды (cgroup v2)."""
    cid = subprocess.run(
        ["docker", "inspect", "-f", "{{.Id}}", CONTAINER],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    path = Path(f"/sys/fs/cgroup/system.slice/docker-{cid}.scope/cpu.stat")
    for line in path.read_text().splitlines():
        if line.startswith("usage_usec"):
            return int(line.split()[1])
    raise RuntimeError("usage_usec не найден")


async def main() -> None:
    """Прогнать операции и напечатать CPU клиента и сервера на запрос."""
    variants = [v for v in build_variants() if v.name != "ferronit-rawdb"]
    for v in variants:
        await v.setup()

    print(f"{'слой':<18}{'операция':<10}{'клиент мкс':>12}{'сервер мкс':>12}{'запросов/с':>12}")
    print("-" * 64)
    for v in variants:
        for op, iters in CASES:
            await seed(5000 if op == "delete_1" else 1000)
            await asyncio.sleep(2)                      # снять остаточный I/O
            cpu0 = time.process_time()
            srv0 = cgroup_cpu_us()
            start = time.perf_counter()
            run = v.run
            for i in range(iters):
                await run(op, i)
            wall = time.perf_counter() - start
            cli = (time.process_time() - cpu0) / iters * 1e6
            srv = (cgroup_cpu_us() - srv0) / iters
            print(f"{v.name:<18}{op:<10}{cli:>12.0f}{srv:>12.0f}{iters / wall:>12,.0f}")
    for v in variants:
        await v.close()


if __name__ == "__main__":
    asyncio.run(main())
