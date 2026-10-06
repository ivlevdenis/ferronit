#!/usr/bin/env bash
# A/B: упирается ли чтение Go-сервиса в кодировщик JSON.
# std = encoding/json (reflection), sonic = быстрый энкодер. Всё остальное одинаково.
set -uo pipefail
cd "$(dirname "$0")"
BIN=./go_pg_bench

run_one() {
  local json=$1 pool=$2 port=$3
  GOMAXPROCS=8 "$BIN" --serve --mode pgx --port "$port" --pool "$pool" --json "$json" >/dev/null 2>&1 &
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
    echo "json=$json пул=$pool: сервис не поднялся" >&2
    kill "$pid" 2>/dev/null || true
    return 1
  fi
  local get post
  get=$(ab -n 5000 -c 50 -k "http://127.0.0.1:$port/users" 2>/dev/null | awk '/^Requests per second/ {print $4}')
  post=$(ab -n 3000 -c 50 -k -p /tmp/_bench_pg_post.json -T application/json \
        "http://127.0.0.1:$port/users" 2>/dev/null | awk '/^Requests per second/ {print $4}')
  printf 'json=%-5s пул=%-3s  чтение %9s   запись %9s\n' "$json" "$pool" "$get" "$post"
  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
  sleep 1
}

printf '{"name": "bench", "email": "bench@example.com"}' > /tmp/_bench_pg_post.json

for _ in 1 2 3; do
  run_one std   16 8311
  run_one sonic 16 8312
  run_one std   64 8313
  run_one sonic 64 8314
done
