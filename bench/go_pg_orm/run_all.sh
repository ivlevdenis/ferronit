#!/usr/bin/env bash
# Замер Go-стенда: слои доступа (pgx / sqlx / GORM) и HTTP-сервис.
# Протокол тот же, что у Python- и Rust-стендов:
#   слои — 10 клиентов × 1000 запросов; HTTP — ab -c 50 -k, GET 5000, POST 3000, ping 10000.
#
# Запуск: ./run_all.sh

set -uo pipefail
cd "$(dirname "$0")"

BIN=./go_pg_bench
BODY=/tmp/_bench_pg_post.json
printf '{"name": "bench", "email": "bench@example.com"}' > "$BODY"

echo "# Go: слои доступа, PostgreSQL 18.6 в Docker"
for procs in 1 8; do
  for op in select insert; do
    for mode in pgx sqlx gorm ent; do
      GOMAXPROCS=$procs "$BIN" --mode "$mode" --op "$op" --clients 10 --requests 1000
    done
  done
done

echo
echo "# Go: HTTP-сервис (net/http), ab -c 50 -k"

run_http() {
  local mode=$1 procs=$2 port=$3 pool=$4 json=${5:-std}
  GOMAXPROCS=$procs "$BIN" --serve --mode "$mode" --port "$port" --pool "$pool" --json "$json" &
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

  local get post ping
  get=$(ab -n 5000 -c 50 -k "http://127.0.0.1:$port/users" 2>/dev/null | awk '/^Requests per second/ {print $4}')
  post=$(ab -n 3000 -c 50 -k -p "$BODY" -T application/json \
        "http://127.0.0.1:$port/users" 2>/dev/null | awk '/^Requests per second/ {print $4}')
  ping=$(ab -n 10000 -c 50 -k "http://127.0.0.1:$port/ping" 2>/dev/null | awk '/^Requests per second/ {print $4}')

  printf 'Go   %-5s GOMAXPROCS=%-2s пул=%-3s json=%-5s  GET /users %9s   POST /users %9s   GET /ping %9s\n' \
    "$mode" "$procs" "$pool" "$json" "$get" "$post" "$ping"

  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
  sleep 1
}

run_http pgx  1 8301 16 std
run_http pgx  8 8302 16 std
run_http pgx  8 8303 64 std
run_http pgx  8 8304 64 sonic   # проверка: упирается ли чтение в encoding/json
run_http gorm 1 8305 16 std
run_http gorm 8 8306 16 std
run_http ent  1 8307 16 std
run_http ent  8 8308 16 std
