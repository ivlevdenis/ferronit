#!/usr/bin/env bash
# Замер вариантов сборки ответа: один сервер (granian), одно окно, лучшее из 3.
# Все варианты читают ОДНУ таблицу тем же путём, различается только сборка ответа.
set -u
ROOT=$HOME/Projects/ferronit
VENV=$ROOT/.venv/bin/python
cd "$ROOT" || exit 1

# DSN берём из стенда, не печатая его.
export DSN=$(grep -m1 -o 'postgresql://[^"]*' bench/api_bench.py)
export BENCH_DSN="$DSN"
ROWS_SEED=1000
N_PING=20000
N_ROWS=20000
N_BIG=10000

"$VENV" - "$ROWS_SEED" "$DSN" <<'PY'
import asyncio, sys
import asyncpg

rows, dsn = int(sys.argv[1]), sys.argv[2]


async def main() -> None:
    conn = await asyncpg.connect(dsn)
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS bench_rows ("
        "id serial PRIMARY KEY, name text NOT NULL, email text NOT NULL)"
    )
    await conn.execute("TRUNCATE bench_rows RESTART IDENTITY")
    await conn.execute(
        "INSERT INTO bench_rows (name, email) "
        "SELECT 'user_' || g, 'u' || g || '@example.com' FROM generate_series(1, $1) AS g",
        rows,
    )
    print(f"  bench_rows: {await conn.fetchval('select count(*) from bench_rows')} строк")
    await conn.close()


asyncio.run(main())
PY

SPECS=(
  "8351|serdes_bench.app_framework:app|framework (как сейчас)"
  "8352|serdes_bench.app_noescape:app|без экранирования"
  "8353|serdes_bench.app_rust:app|Rust-писатель"
  "8354|serdes_bench.app_cached:app|cached (потолок модели)"
  "8355|serdes_bench.app_jsonsql:app|json_agg (потолок ser)"
)

PIDS=()
cleanup() { for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null; done; }
trap cleanup EXIT

for spec in "${SPECS[@]}"; do
  IFS='|' read -r port app _label <<<"$spec"
  "$VENV" -m granian --interface asgi --no-ws --host 127.0.0.1 --port "$port" --workers 1 \
    --log-level warning --working-dir bench "$app" &
  PIDS+=($!)
done

echo "--- проверка готовности и паритет тел ---"
for spec in "${SPECS[@]}"; do
  IFS='|' read -r port _app label <<<"$spec"
  for _try in $(seq 1 60); do
    curl -sf --max-time 2 "localhost:$port/ping" >/dev/null 2>&1 && break
    sleep 0.5
  done
  code=$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' "localhost:$port/ping")
  body100=$(curl -s --max-time 5 "localhost:$port/rows?limit=100" | md5sum | cut -c1-12)
  size=$(curl -s --max-time 5 "localhost:$port/rows?limit=100" | wc -c)
  printf '  %-24s :%s код=%s тело(limit=100) md5=%s байт=%s\n' "$label" "$port" "$code" "$body100" "$size"
done

best3() {
  local _
  for _ in 1 2 3; do
    ab -n "$3" -c 50 -k -q "localhost:$1$2" 2>/dev/null | awk '/Requests per second/{print $4}'
  done | sort -g | tail -1
}

echo
echo "granian 1 воркер · ab -c 50 -k · лучшее из 3 · bench_rows=$ROWS_SEED · load:$(cut -d' ' -f1 /proc/loadavg)"
printf '  %-24s %10s %18s %18s\n' "вариант" "/ping" "/rows?limit=100" "/rows?limit=$ROWS_SEED"
for spec in "${SPECS[@]}"; do
  IFS='|' read -r port _app label <<<"$spec"
  ab -n 2000 -c 50 -k -q "localhost:$port/ping" >/dev/null 2>&1
  ab -n 2000 -c 50 -k -q "localhost:$port/rows?limit=100" >/dev/null 2>&1
  ab -n 1000 -c 50 -k -q "localhost:$port/rows?limit=$ROWS_SEED" >/dev/null 2>&1
  printf '  %-24s %10s %18s %18s\n' "$label" \
    "$(best3 "$port" "/ping" "$N_PING")" \
    "$(best3 "$port" "/rows?limit=100" "$N_ROWS")" \
    "$(best3 "$port" "/rows?limit=$ROWS_SEED" "$N_BIG")"
done
