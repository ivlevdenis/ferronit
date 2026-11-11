#!/usr/bin/env bash
# Test databases for the data layer: start / check / stop.
#
#   ./scripts/dbs_up.sh up        — start MySQL and wait for its healthcheck
#   ./scripts/dbs_up.sh status    — what is running
#   ./scripts/dbs_up.sh down      — stop (test data is not needed)
#
# SQLite needs no container (the library runs in-process) — it is covered by
# tests/test_sql_backends.py via aiosqlite.
set -eu
cd "$(dirname "$0")/.."
COMPOSE="docker-compose.dbs.yml"

case "${1:-up}" in
  up)
    docker compose -f "$COMPOSE" up -d --wait
    echo "MySQL is ready: 127.0.0.1:3307 (root/root, db ferronit_test)"
    ;;
  status)
    docker compose -f "$COMPOSE" ps
    ;;
  down)
    docker compose -f "$COMPOSE" down
    ;;
  *)
    echo "usage: $0 [up|status|down]" >&2
    exit 2
    ;;
esac
