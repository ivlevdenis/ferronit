"""Полный замер: Ferronit B (granian) против Tornado/Twisted (свой сервер).

ping + 5 limit'ов (1/20/50/100/1000) + 4 записи (INSERT/UPDATE/UPSERT/DELETE).
ab -c 50 -k, лучшее из 3, таблица bench_rows — 1000 строк. Чтения и записи —
на свежих серверах (по фазе), перед каждой записью и перед каждым прогоном записи
re-seed, чтобы UPDATE/UPSERT/DELETE били по существующей строке.
"""

import asyncio
import re
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PY = str(ROOT / ".venv" / "bin" / "python")
DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"

RUNS = 3
PING_N = 20000
SEL_N = 20000
SEL_BIG_N = 10000
WRITE_N = 3000

LIMITS = (1, 20, 50, 100, 1000)
INSERT_BODY = '{"name":"bench","email":"bench@example.com"}'
UPSERT_BODY = '{"id":1,"name":"bench","email":"bench@example.com"}'
WRITES = [
    ("INSERT", "POST", "/rows", INSERT_BODY),
    ("UPDATE", "PUT", "/rows/1", INSERT_BODY),
    ("UPSERT", "POST", "/rows/upsert", UPSERT_BODY),
    ("DELETE", "DELETE", "/rows/1", None),
]

VARIANTS = {
    "Ferronit B (granian)": {
        "port": 8321,
        "cmd": [PY, "-m", "granian", "--interface", "asgi", "--no-ws",
                "--host", "127.0.0.1", "--port", "8321", "--workers", "1",
                "--log-level", "warning",
                "django_bench.ferronit_service:app", "--working-dir", "bench"],
    },
    "Tornado (свой сервер)": {
        "port": 8246,
        "cmd": [PY, "bench/tornado_bench/app.py"],
    },
    "Twisted (свой сервер)": {
        "port": 8247,
        "cmd": [PY, "bench/twisted_bench/app.py"],
    },
}


def seed():
    async def _seed():
        import asyncpg
        c = await asyncpg.connect(DSN)
        try:
            await c.execute("CREATE TABLE IF NOT EXISTS bench_rows "
                            "(id serial PRIMARY KEY, name text NOT NULL, email text NOT NULL)")
            await c.execute("TRUNCATE bench_rows RESTART IDENTITY")
            await c.execute("INSERT INTO bench_rows (name, email) "
                            "SELECT 'user_' || g, 'u' || g || '@example.com' "
                            "FROM generate_series(1, 1000) AS g")
        finally:
            await c.close()
    asyncio.run(_seed())


def wait_ready(port, timeout=20.0):
    import urllib.request
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=1) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.2)
    raise TimeoutError(f":{port} не поднялся")


def ab_rps(port, path, n):
    out = subprocess.run(["ab", "-n", str(n), "-c", "50", "-k",
                          f"http://127.0.0.1:{port}{path}"],
                         capture_output=True, text=True, timeout=300).stdout
    m = re.search(r"Requests per second:\s+([\d.]+)", out)
    if not m:
        raise RuntimeError(f"ab без результата для {path}:\n{out[-400:]}")
    return float(m.group(1))


def ab_write(port, path, method, body, n):
    body_file = HERE / "_bench_write.json"
    cmd = ["ab", "-n", str(n), "-c", "50"]
    if method == "POST":
        body_file.write_text(body)
        cmd += ["-k", "-T", "application/json", "-p", str(body_file)]
    elif method == "PUT":
        body_file.write_text(body)
        cmd += ["-k", "-T", "application/json", "-u", str(body_file)]
    elif method == "DELETE":
        # без -k: Tornado вешает keep-alive DELETE (запрос без тела) под ab -k
        cmd += ["-m", "DELETE"]
    cmd.append(f"http://127.0.0.1:{port}{path}")
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=300).stdout
    m = re.search(r"Requests per second:\s+([\d.]+)", out)
    if not m:
        raise RuntimeError(f"ab без результата для {method} {path}:\n{out[-400:]}")
    return float(m.group(1))


def best_of(fn):
    """Лучшее из RUNS прогонов; временный сбой ab пережидает и повторяет."""
    best = 0.0
    for _ in range(RUNS):
        v = None
        for _ in range(3):
            try:
                v = fn()
                break
            except RuntimeError:
                time.sleep(0.5)
        if v is None:
            raise RuntimeError("ab стабильно не даёт результат после 3 попыток")
        best = max(best, v)
    return best


def start(cfg):
    return subprocess.Popen(cfg["cmd"], cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def measure(name, cfg):
    port = cfg["port"]
    row = {}
    # чтения
    proc = start(cfg)
    try:
        wait_ready(port)
        row["ping"] = best_of(lambda: ab_rps(port, "/ping", PING_N))
        for lim in LIMITS:
            n = SEL_BIG_N if lim == 1000 else SEL_N
            row[f"limit={lim}"] = best_of(lambda: ab_rps(port, f"/rows?limit={lim}", n))
    finally:
        proc.terminate()
        proc.wait()
    # записи (свежий сервер, re-seed перед каждым прогоном)
    proc = start(cfg)
    try:
        wait_ready(port)
        for opkey, method, path, body in WRITES:
            def run_write(method=method, path=path, body=body):
                seed()
                return ab_write(port, path, method, body, WRITE_N)
            row[opkey] = best_of(run_write)
    finally:
        proc.terminate()
        proc.wait()
    return row


def main():
    seed()
    results = {}
    for name, cfg in VARIANTS.items():
        results[name] = measure(name, cfg)
        r = results[name]
        print(f"{name}: ping {r['ping']:,.0f}, limit100 {r['limit=100']:,.0f}, "
              f"INSERT {r['INSERT']:,.0f}, DELETE {r['DELETE']:,.0f}", flush=True)

    keys = ["ping"] + [f"limit={l}" for l in LIMITS] + ["INSERT", "UPDATE", "UPSERT", "DELETE"]
    header = ["показатель (req/s)"] + list(VARIANTS)
    lines = ["| " + " | ".join(header) + " |",
             "|" + "---|" * len(header)]
    labels = {"ping": "GET /ping", **{f"limit={l}": f"GET /rows?limit={l}" for l in LIMITS},
              "INSERT": "POST /rows (INSERT)", "UPDATE": "PUT /rows/1 (UPDATE)",
              "UPSERT": "POST /rows/upsert", "DELETE": "DELETE /rows/1"}
    for k in keys:
        cells = [labels[k]] + [f"{results[v][k]:,.0f}".replace(",", " ") for v in VARIANTS]
        lines.append("| " + " | ".join(cells) + " |")

    md = ("# Полный замер: Ferronit против Tornado и Twisted\n\n"
          "ab -c 50 -k, лучшее из 3, таблица bench_rows — 1000 строк, одна машина.\n"
          "Tornado/Twisted — на своём сервере (не ASGI, не granian); Ferronit — granian, 1 воркер.\n\n"
          + "\n".join(lines) + "\n")
    out = HERE / "RESULTS_tornado_twisted.md"
    out.write_text(md)
    print(f"\nзаписано: {out}")


if __name__ == "__main__":
    main()
