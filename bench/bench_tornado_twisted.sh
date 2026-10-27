#!/usr/bin/env bash
# Замер tornado / twisted (свой сервер) против Ferronit B (granian), один прогон.
# ab -c 50 -k, лучшее из 3, таблица bench_rows — 1000 строк.
# ВАЖНО: серверы гасятся по PID из $! (не pkill — совпадёт со своей командной строкой),
# команду запуска не оборачивать в функцию (иначе $! = subshell, сервер осиротеет).
set -u

cd "$(dirname "$0")/.."   # корень репо
PY=.venv/bin/python

# ── сидирование: ровно 1000 строк ─────────────────────────────────────
$PY - <<'PY'
import asyncio, asyncpg
DSN = 'postgresql://postgres:postgres@127.0.0.1:5432/postgres'
async def m():
    c = await asyncpg.connect(DSN)
    await c.execute('CREATE TABLE IF NOT EXISTS bench_rows (id serial PRIMARY KEY, name text NOT NULL, email text NOT NULL)')
    await c.execute('TRUNCATE bench_rows RESTART IDENTITY')
    await c.execute("INSERT INTO bench_rows (name, email) SELECT 'user_' || g, 'u' || g || '@example.com' FROM generate_series(1, 1000) AS g")
    n = await c.fetchval('select count(*) from bench_rows')
    print(f'  bench_rows count = {n}')
    await c.close()
asyncio.run(m())
PY

declare -A PORT=( [ferronit]=8321 [tornado]=8246 [twisted]=8247 )

wait_ready() {  # $1=port
  for _ in $(seq 1 100); do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "http://127.0.0.1:$1/ping" 2>/dev/null)
    [ "$code" = "200" ] && return 0
    sleep 0.2
  done
  echo "  !! сервер на :$1 не поднялся" >&2
  return 1
}

free_port() {  # $1=port — дождаться, пока порт освободится
  for _ in $(seq 1 50); do
    curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$1/ping" 2>/dev/null || return 0
    sleep 0.2
  done
}

ab_best() {  # $1=port $2=path $3=n — лучшее из 3
  local best=0 v
  for _ in 1 2 3; do
    v=$(ab -n "$3" -c 50 -k "http://127.0.0.1:$1$2" 2>/dev/null \
        | grep -oP 'Requests per second:\s+\K[\d.]+' | head -1)
    awk -v a="$best" -v b="${v:-0}" 'BEGIN{print (a>b?a:b)}' > /tmp/_best
    best=$(cat /tmp/_best)
  done
  echo "$best"
}

run_one() {  # $1=name — запуск простой командой с &, PID из $!
  local name=$1 port=${PORT[$name]} PID
  echo "== $name (:$port) =="
  case "$name" in
    ferronit) $PY -m granian --interface rsgi --host 127.0.0.1 --port "$port" \
                --workers 1 --log-level warning django_bench.ferronit_service:app --working-dir bench & ;;
    tornado)  $PY bench/tornado_bench/app.py & ;;
    twisted)  $PY bench/twisted_bench/app.py & ;;
  esac
  PID=$!
  wait_ready "$port" || { kill "$PID" 2>/dev/null; return; }
  local p100 r100 r1000
  p100=$(ab_best "$port" "/ping" 20000)
  r100=$(ab_best "$port" "/rows?limit=100" 20000)
  r1000=$(ab_best "$port" "/rows?limit=1000" 10000)
  printf '  ping            %s req/s\n' "$p100"
  printf '  rows?limit=100  %s req/s\n' "$r100"
  printf '  rows?limit=1000 %s req/s\n' "$r1000"
  kill "$PID" 2>/dev/null
  wait "$PID" 2>/dev/null
  free_port "$port"
}

trap 'echo "прервано"; exit 130' INT TERM

echo "сервер: granian (ferronit) / собственный (tornado, twisted); ab -c 50 -k, лучшее из 3"
for name in ferronit tornado twisted; do
  run_one "$name"
done
