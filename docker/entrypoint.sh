#!/usr/bin/env sh
# Запуск ASGI-приложения Ferrox в контейнере.
#
#   APP=demo_app:app        модуль:атрибут (по умолчанию)
#   SERVER=granian|uvicorn  бэкенд (granian по умолчанию, как `ferrox run`)
#   HOST=0.0.0.0
#   PORT=8000
#   WORKERS=1               число воркеров (granian/uvicorn)
#   RELOAD=0               1 — режим разработки с авто-перезагрузкой (uvicorn)
set -eu

APP="${APP:-demo_app:app}"
SERVER="${SERVER:-granian}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
WORKERS="${WORKERS:-1}"
RELOAD="${RELOAD:-0}"

if [ "$#" -gt 0 ]; then
    # Явная команда имеет приоритет: docker run ferrox:0.8.0 <cmd>
    exec "$@"
fi

echo "ferrox: server=${SERVER} app=${APP} host=${HOST} port=${PORT} workers=${WORKERS}"

if [ "$SERVER" = "uvicorn" ]; then
    if [ "$RELOAD" = "1" ]; then
        exec python -m uvicorn --host "$HOST" --port "$PORT" --reload "$APP"
    fi
    exec python -m uvicorn --host "$HOST" --port "$PORT" --workers "$WORKERS" "$APP"
fi

exec python -m granian \
    --interface asgi \
    --host "$HOST" \
    --port "$PORT" \
    --workers "$WORKERS" \
    --no-ws \
    "$APP"
