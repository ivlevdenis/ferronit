#!/usr/bin/env bash
# Замер Go-стенда на том же контракте, что и api_bench.py (Python-стенд):
#   GET /ping, GET /rows?limit=N (1/20/50/100/1000), POST /rows, PUT /rows/{id},
#   DELETE /rows/{id}, POST /rows/upsert. Таблица — bench_rows.
# Слои доступа (pgx/sqlx/gorm/ent) — 10 клиентов × 1000 запросов, select/insert.
# Запуск: ./run_all.sh

set -uo pipefail
cd "$(dirname "$0")"

BIN=./go_pg_bench
BODY=/tmp/_bench_pg_post.json
UPBODY=/tmp/_bench_pg_upsert.json
printf '{"name": "bench", "email": "bench@example.com"}' > "$BODY"
printf '{"id": 1, "name": "bench", "email": "bench@example.com"}' > "$UPBODY"

seed() {
  docker exec velox-pg psql -U postgres -d postgres -q \
    -c "TRUNCATE bench_rows RESTART IDENTITY; INSERT INTO bench_rows (name,email) SELECT 'user_'||g,'u'||g||'@example.com' FROM generate_series(1,1000) g;" >/dev/null 2>&1
}

echo "# Go: слои доступа, PostgreSQL 18.6 в Docker (таблица bench_rows)"
for procs in 1 8; do
  for op in select insert; do
    for mode in pgx sqlx gorm ent; do
      GOMAXPROCS=$procs "$BIN" --mode "$mode" --op "$op" --clients 10 --requests 1000
    done
  done
done

echo
echo "# Go: HTTP-сервис (net/http), ab -c 50 -k, контракт api_bench.py"

run_http() {
  local mode=$1 procs=$2 port=$3
  GOMAXPROCS=$procs "$BIN" --serve --mode "$mode" --port "$port" --pool 16 &
  local pid=$!
  local ready=""
  for _ in $(seq 1 60); do
    if env -u ALL_PROXY -u HTTPS_PROXY -u HTTP_PROXY curl -s -m 1 --noproxy '*' \
        -o /dev/null "http://127.0.0.1:$port/ping" 2>/dev/null; then
      ready=1
      break
    fi
    sleep 0.25
  done
  if [ -z "$ready" ]; then
    echo "Go $mode: сервис не поднялся на порту $port" >&2
    kill "$pid" 2>/dev/null || true
    return 1
  fi

  local ping r1 r20 r50 r100 r1000 ins upd ups del
  ping=$(ab -n 10000 -c 50 -k "http://127.0.0.1:$port/ping" 2>/dev/null | awk '/^Requests per second/ {print $4}')
  r1=$(ab -n 5000 -c 50 -k "http://127.0.0.1:$port/rows?limit=1" 2>/dev/null | awk '/^Requests per second/ {print $4}')
  r20=$(ab -n 5000 -c 50 -k "http://127.0.0.1:$port/rows?limit=20" 2>/dev/null | awk '/^Requests per second/ {print $4}')
  r50=$(ab -n 5000 -c 50 -k "http://127.0.0.1:$port/rows?limit=50" 2>/dev/null | awk '/^Requests per second/ {print $4}')
  r100=$(ab -n 5000 -c 50 -k "http://127.0.0.1:$port/rows?limit=100" 2>/dev/null | awk '/^Requests per second/ {print $4}')
  r1000=$(ab -n 5000 -c 50 -k "http://127.0.0.1:$port/rows?limit=1000" 2>/dev/null | awk '/^Requests per second/ {print $4}')
  seed; ins=$(ab -n 3000 -c 50 -k -p "$BODY" -T application/json "http://127.0.0.1:$port/rows" 2>/dev/null | awk '/^Requests per second/ {print $4}')
  seed; upd=$(ab -n 3000 -c 50 -k -u "$BODY" -T application/json "http://127.0.0.1:$port/rows/1" 2>/dev/null | awk '/^Requests per second/ {print $4}')
  seed; ups=$(ab -n 3000 -c 50 -k -p "$UPBODY" -T application/json "http://127.0.0.1:$port/rows/upsert" 2>/dev/null | awk '/^Requests per second/ {print $4}')
  seed; del=$(ab -n 3000 -c 50 -k -m DELETE "http://127.0.0.1:$port/rows/1" 2>/dev/null | awk '/^Requests per second/ {print $4}')

  printf 'Go %-5s GOMAXPROCS=%-2s ping=%8s rows1=%7s rows20=%7s rows50=%7s rows100=%7s rows1000=%7s ins=%7s upd=%6s ups=%6s del=%7s\n' \
    "$mode" "$procs" "$ping" "$r1" "$r20" "$r50" "$r100" "$r1000" "$ins" "$upd" "$ups" "$del"

  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
  sleep 1
}

# GOMAXPROCS=1 — честное сравнение с Ferronit (1 процесс)
run_http pgx  1 8301
run_http sqlx 1 8302
run_http gorm 1 8303
run_http ent  1 8304
# GOMAXPROCS=8 — несколько ядер (дополнительная точка)
run_http pgx  8 8305
