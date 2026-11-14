#!/usr/bin/env sh
# Run a Ferronit ASGI application inside a container.
#
#   APP=demo_app:app        module:attribute (default)
#   SERVER=granian|uvicorn  backend (granian by default, like `ferronit run`)
#   HOST=0.0.0.0
#   PORT=8000
#   WORKERS=1               number of workers (granian/uvicorn)
#   RELOAD=0                1 — development mode with auto-reload (uvicorn)
set -eu

APP="${APP:-demo_app:app}"
SERVER="${SERVER:-granian}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
WORKERS="${WORKERS:-1}"
RELOAD="${RELOAD:-0}"

if [ "$#" -gt 0 ]; then
    # An explicit command takes priority: docker run ferronit:0.25.0 <cmd>
    exec "$@"
fi

echo "ferronit: server=${SERVER} app=${APP} host=${HOST} port=${PORT} workers=${WORKERS}"

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
