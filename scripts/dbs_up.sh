#!/usr/bin/env bash
# Тестовые БД для слоя данных: поднять / проверить / остановить.
#
#   ./scripts/dbs_up.sh up        — поднять MySQL и дождаться healthcheck
#   ./scripts/dbs_up.sh status    — что запущено
#   ./scripts/dbs_up.sh down      — остановить (данные тестов не нужны)
#
# SQLite контейнера не требует (библиотека в процессе) — он проверяется тестами
# tests/test_sql_backends.py через aiosqlite.
set -eu
cd "$(dirname "$0")/.."
COMPOSE="docker-compose.dbs.yml"

case "${1:-up}" in
  up)
    docker compose -f "$COMPOSE" up -d --wait
    echo "MySQL готова: 127.0.0.1:3307 (root/root, база ferronit_test)"
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
