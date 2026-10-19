"""CPU на запрос для вариантов сборки ответа (ser/deser модели) — в мкс, под ``ab``.

Дополняет ``run_ab.sh`` (тот даёт req/s): здесь видно, сколько CPU запрос стоит
серверу, когда результат сериализуется по-разному. Разница «framework − cached» и есть
цена сериализации/десериализации модели в запросе.

    сервер     — CPU процесса приложения и его потомков, /proc/<pid>/stat
    PostgreSQL — CPU контейнера с базой (cgroup v2, usage_usec)
    ab         — CPU клиента (os.wait4 → rusage)

Запуск: .venv/bin/python bench/serdes_bench/cpu_bench.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
sys.path.insert(0, str(ROOT / "bench"))

import asyncpg  # noqa: E402

CLK = os.sysconf("SC_CLK_TCK")
CONTAINER = "velox-pg"
SEED_ROWS = 1000

VARIANTS = (
    (8351, "serdes_bench.app_framework:app", "framework"),
    (8352, "serdes_bench.app_noescape:app", "noescape"),
    (8353, "serdes_bench.app_rust:app", "rust"),
    (8354, "serdes_bench.app_cached:app", "cached"),
    (8355, "serdes_bench.app_jsonsql:app", "jsonsql"),
)
# (метка, путь, запросов)
SCENARIOS = (
    ("ping", "/ping", 10_000),
    ("rows?limit=100", "/rows?limit=100", 8_000),
    ("rows?limit=1000", "/rows?limit=1000", 4_000),
)


def dsn_from_bench() -> str:
    """Строка подключения — та же, что у остальных стендов (не печатается)."""
    text = (ROOT / "bench" / "api_bench.py").read_text(encoding="utf-8")
    match = re.search(r'"(postgresql://[^"]+)"', text)
    if not match:
        raise RuntimeError("DSN не найден в bench/api_bench.py")
    return match.group(1)


def stat_cpu_ticks(pid: int) -> int:
    """utime+stime процесса в тиках; 0, если процесс уже умер."""
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as fh:
            fields = fh.read().rsplit(") ", 1)[1].split()
    except OSError:
        return 0
    return int(fields[11]) + int(fields[12])


def children(pid: int) -> list[int]:
    """Дочерние процессы (по ppid из /proc/*/stat)."""
    kids: list[int] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", encoding="ascii") as fh:
                fields = fh.read().rsplit(") ", 1)[1].split()
        except OSError:
            continue
        if int(fields[1]) == pid:
            kids.append(int(entry))
    return kids


def tree_cpu_ticks(pid: int) -> int:
    """CPU процесса и всех его потомков, в тиках."""
    total = stat_cpu_ticks(pid)
    for kid in children(pid):
        total += tree_cpu_ticks(kid)
    return total


def pg_cpu_us() -> int:
    """CPU контейнера с PostgreSQL, микросекунды (cgroup v2)."""
    cid = subprocess.run(
        ["docker", "inspect", "-f", "{{.Id}}", CONTAINER],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    path = Path(f"/sys/fs/cgroup/system.slice/docker-{cid}.scope/cpu.stat")
    for line in path.read_text().splitlines():
        if line.startswith("usage_usec"):
            return int(line.split()[1])
    raise RuntimeError("usage_usec не найден")


def seed(dsn: str) -> None:
    """Наполняет ``bench_rows`` ровно 1000 строками (иначе limit=100 покажет пустоту)."""
    import asyncio

    async def main() -> None:
        conn = await asyncpg.connect(dsn)
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS bench_rows ("
            "id serial PRIMARY KEY, name text NOT NULL, email text NOT NULL)"
        )
        await conn.execute("TRUNCATE bench_rows RESTART IDENTITY")
        await conn.execute(
            "INSERT INTO bench_rows (name, email) "
            "SELECT 'user_' || g, 'u' || g || '@example.com' "
            "FROM generate_series(1, $1) AS g",
            SEED_ROWS,
        )
        await conn.close()

    asyncio.run(main())


def start(port: int, app: str, dsn: str) -> subprocess.Popen:
    """Поднимает granian с одним воркером в каталоге bench/."""
    return subprocess.Popen(
        [
            PY, "-m", "granian", "--interface", "asgi", "--no-ws",
            "--host", "127.0.0.1", "--port", str(port), "--workers", "1",
            "--log-level", "warning", "--working-dir", "bench", app,
        ],
        cwd=str(ROOT),
        env={**os.environ, "BENCH_DSN": dsn},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_ready(port: int) -> None:
    """Ждёт готовности сервиса по /ping."""
    for _ in range(80):
        probe = subprocess.run(
            ["curl", "-sf", "--max-time", "2", f"localhost:{port}/ping"],
            capture_output=True,
        )
        if probe.returncode == 0:
            return
        time.sleep(0.5)
    raise RuntimeError(f"сервис на :{port} не поднялся")


def run_ab(port: int, path: str, n: int) -> tuple[float, float]:
    """Прогон ab; возвращает (запросов/с, CPU клиента в секундах)."""
    out_file = Path(tempfile.gettempdir()) / "serdes_ab_out.txt"
    with out_file.open("w") as fh:
        proc = subprocess.Popen(
            ["ab", "-n", str(n), "-c", "50", "-k", f"http://127.0.0.1:{port}{path}"],
            stdout=fh,
            stderr=subprocess.DEVNULL,
        )
        _, _status, usage = os.wait4(proc.pid, 0)
    text = out_file.read_text()
    match = re.search(r"Requests per second:\s+([\d.]+)", text)
    if not match:
        raise RuntimeError(f"ab не дал результат для {path}: {text[-300:]}")
    return float(match.group(1)), usage.ru_utime + usage.ru_stime  # type: ignore[attr-defined]


def main() -> None:
    """Снимает CPU-профиль по вариантам и сценариям."""
    dsn = dsn_from_bench()
    seed(dsn)
    print(
        f"# CPU на запрос (ab -c 50 -k), {time.strftime('%Y-%m-%d %H:%M')}, "
        f"ядер {os.cpu_count()}, load {os.getloadavg()[0]:.2f}, bench_rows={SEED_ROWS}"
    )
    print(f"{'вариант':<11}{'сценарий':<16}{'req/s':>9}{'сервер мкс':>12}{'PG мкс':>9}{'ab мкс':>9}")
    print("-" * 66)
    for port, app, label in VARIANTS:
        proc = start(port, app, dsn)
        try:
            wait_ready(port)
            for scen, path, n in SCENARIOS:
                run_ab(port, path, 1000)  # прогрев
                app0, pg0 = tree_cpu_ticks(proc.pid), pg_cpu_us()
                req_s, ab_cpu = run_ab(port, path, n)
                app_cpu = (tree_cpu_ticks(proc.pid) - app0) / CLK
                pg_cpu = (pg_cpu_us() - pg0) / 1e6
                print(
                    f"{label:<11}{scen:<16}{req_s:>9,.0f}{app_cpu / n * 1e6:>12.1f}"
                    f"{pg_cpu / n * 1e6:>9.1f}{ab_cpu / n * 1e6:>9.1f}"
                )
        finally:
            proc.terminate()
            proc.wait()
    print(
        "\nсервер мкс = CPU процесса приложения и потомков на запрос; "
        "разница framework−cached = цена ser/deser модели в запросе"
    )


if __name__ == "__main__":
    main()
