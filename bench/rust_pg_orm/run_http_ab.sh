#!/usr/bin/env bash
# Замер Rust HTTP-сервиса тем же протоколом, что bench_postgres.py:
#   ab -n N -c 50 -k,  GET /users 5000, POST /users 3000, GET /ping 10000.
# Тело POST — как в Python-бенче (_bench_pg_post.json).
#
# Запуск: ./run_http_ab.sh

set -uo pipefail
cd "$(dirname "$0")"

BIN=./target/release/http_service
BODY=/tmp/_bench_pg_post.json
printf '{"name": "bench", "email": "bench@example.com"}' > "$BODY"

run_config() {
  local mode=$1 threads=$2 port=$3 pool=$4
  "$BIN" --mode "$mode" --threads "$threads" --port "$port" --pool "$pool" &
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
    echo "Rust $mode: сервис не поднялся на порту $port" >&2
    kill "$pid" 2>/dev/null || true
    return 1
  fi

  local get post ping
  get=$(ab -n 5000 -c 50 -k "http://127.0.0.1:$port/users" 2>/dev/null | awk '/^Requests per second/ {print $4}')
  post=$(ab -n 3000 -c 50 -k -p "$BODY" -T application/json \
        "http://127.0.0.1:$port/users" 2>/dev/null | awk '/^Requests per second/ {print $4}')
  ping=$(ab -n 10000 -c 50 -k "http://127.0.0.1:$port/ping" 2>/dev/null | awk '/^Requests per second/ {print $4}')

  printf 'Rust %-7s потоков=%-2s пул=%-3s  GET /users %9s   POST /users %9s   GET /ping %9s\n' \
    "$mode" "$threads" "$pool" "$get" "$post" "$ping"

  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
  sleep 1
}

echo "# Rust HTTP-сервис (axum), PostgreSQL 18.6 в Docker, ab -c 50 -k"
run_config raw     1 8201 16
run_config raw     8 8202 16
run_config raw     8 8203 64
run_config seaorm  1 8204 16
run_config seaorm  8 8205 16
