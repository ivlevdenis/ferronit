"""CPU под HTTP-нагрузкой: приложение, PostgreSQL и клиент — всё в мкс на запрос.

Дополняет `queries_vs_sqlalchemy.py` (там CPU считался без HTTP, в процессе). Здесь
считается то же, но под нагрузкой ApacheBench на реальных маршрутах:

    сервер      — CPU процесса приложения (включая потомков), /proc/<pid>/stat
    PostgreSQL  — CPU контейнера с базой, cgroup v2 (usage_usec)
    ab          — CPU клиента (os.wait4 → rusage)

Приложения переиспользуются из `api_bench.py` (тот же код, тот же сервер, 1 воркер).

Запуск: .venv/bin/python bench/http_cpu_bench.py
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
sys.path.insert(0, str(HERE))

import api_bench  # noqa: E402

CLK = os.sysconf("SC_CLK_TCK")
CONTAINER = "velox-pg"
APPS = ("ferronit", "blacksheep", "fastapi")
# (метка, метод, путь, тело, запросов)
SCENARIOS = (
    ("ping", "GET", "/ping", None, 10_000),
    ("rows?limit=1", "GET", "/rows?limit=1", None, 5_000),
    ("rows?limit=100", "GET", "/rows?limit=100", None, 5_000),
    ("rows?limit=1000", "GET", "/rows?limit=1000", None, 3_000),
    ("INSERT", "POST", "/rows", '{"name":"bench","email":"bench@example.com"}', 3_000),
)
BODY_FILE = HERE / "_bench_write.json"


def stat_cpu_ticks(pid: int) -> int:
    """utime+stime процесса в тиках; 0, если процесс уже умер."""
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as fh:
            fields = fh.read().rsplit(") ", 1)[1].split()
    except OSError:
        return 0
    return int(fields[11]) + int(fields[12])      # utime, stime


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
        if int(fields[1]) == pid:                 # ppid
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
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    path = Path(f"/sys/fs/cgroup/system.slice/docker-{cid}.scope/cpu.stat")
    for line in path.read_text().splitlines():
        if line.startswith("usage_usec"):
            return int(line.split()[1])
    raise RuntimeError("usage_usec не найден")


def run_ab(port: int, path: str, method: str, body: str | None, n: int) -> tuple[float, float]:
    """Прогнать ab и вернуть (запросов/с, CPU клиента в секундах)."""
    out_file = Path(tempfile.gettempdir()) / "ferronit_ab_cpu_out.txt"
    cmd = ["ab", "-n", str(n), "-c", "50", "-k"]
    if method == "POST":
        BODY_FILE.write_text(body or "")
        cmd += ["-T", "application/json", "-p", str(BODY_FILE)]
    cmd.append(f"http://127.0.0.1:{port}{path}")

    with out_file.open("w") as fh:
        proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.DEVNULL)
        _, _status, usage = os.wait4(proc.pid, 0)
    text = out_file.read_text()
    match = re.search(r"Requests per second:\s+([\d.]+)", text)
    if not match:
        raise RuntimeError(f"ab не дал результат для {method} {path}: {text[-300:]}")
    return float(match.group(1)), usage.ru_utime + usage.ru_stime   # type: ignore[attr-defined]


def main() -> None:
    """Снять CPU-профиль по сценариям для каждого приложения."""
    api_bench.seed()
    print(
        f"# CPU под нагрузкой (ab -c 50 -k), {time.strftime('%Y-%m-%d %H:%M')}, "
        f"ядер {os.cpu_count()}, load {os.getloadavg()[0]:.2f}"
    )
    print(
        f"{'приложение':<12}{'сценарий':<16}{'req/s':>10}{'сервер мкс':>12}"
        f"{'PG мкс':>9}{'ab мкс':>9}{'ядер с+PG':>11}"
    )
    print("-" * 79)
    for app in APPS:
        port = api_bench.PORTS[app]
        proc = api_bench.start(app, port)
        try:
            api_bench.wait_ready(port)
            for label, method, path, body, n in SCENARIOS:
                if method == "POST":
                    api_bench.seed()
                app0 = tree_cpu_ticks(proc.pid)
                pg0 = pg_cpu_us()
                start = time.perf_counter()
                req_s, ab_cpu = run_ab(port, path, method, body, n)
                wall = time.perf_counter() - start
                app_cpu = (tree_cpu_ticks(proc.pid) - app0) / CLK
                pg_cpu = (pg_cpu_us() - pg0) / 1e6
                print(
                    f"{app:<12}{label:<16}{req_s:>10,.0f}"
                    f"{app_cpu / n * 1e6:>12.0f}{pg_cpu / n * 1e6:>9.0f}"
                    f"{ab_cpu / n * 1e6:>9.0f}{(app_cpu + pg_cpu) / wall:>11.2f}"
                )
        finally:
            proc.terminate()
            proc.wait()


if __name__ == "__main__":
    main()
