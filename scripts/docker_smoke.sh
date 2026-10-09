#!/usr/bin/env bash
# Smoke-тест образа Ferronit: поднять контейнер, проверить живое приложение, погасить.
#
#   ./scripts/docker_smoke.sh                 # образ ferronit:0.8.1
#   IMAGE=ferronit:dev PORT=9001 ./scripts/docker_smoke.sh
set -euo pipefail

IMAGE="${IMAGE:-ferronit:0.8.1}"
PORT="${PORT:-8931}"
NAME="ferronit-smoke"

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "==> запуск $IMAGE на порту $PORT"
cleanup
docker run -d --rm --name "$NAME" -p "$PORT:8000" "$IMAGE" >/dev/null

echo "==> ждём готовности (до 30 с)"
for _ in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:$PORT/" >/dev/null 2>&1; then break; fi
    sleep 1
done

fail=0
check() { # check <имя> <ожидаемое-подстрока> <url> [curl-args...]
    local name="$1" expect="$2" url="$3"; shift 3
    local body
    body="$(curl -sf "$@" "$url" || true)"
    if printf '%s' "$body" | grep -q "$expect"; then
        echo "  ok   $name"
    else
        echo "  FAIL $name — ожидалось '$expect', получено: ${body:0:200}"
        fail=1
    fi
}

echo "==> проверки HTTP"
check "GET / (JSON + статус)" '"status":"ok"'      "http://127.0.0.1:$PORT/"
check "GET / (версия)"        '"version":"0.8.1"'  "http://127.0.0.1:$PORT/"
check "GET /json?n=3"         '"count":3'          "http://127.0.0.1:$PORT/json?n=3"
check "POST /echo"            '"received"'         "http://127.0.0.1:$PORT/echo" \
      -X POST -H 'content-type: application/json' -d '{"hello":"world"}'
check "GET /health"           '"status":"ok"'      "http://127.0.0.1:$PORT/health"

echo "==> проверки установленных дистрибутивов в образе"
docker exec "$NAME" python -c "
import importlib.metadata as m
import ferronit, ferronit_core

core = m.version('ferronit-core')
assert ferronit.__version__ == '0.8.1', ferronit.__version__
assert core == ferronit.__version__, (core, ferronit.__version__)
assert hasattr(ferronit_core, 'FerronitApp')
print(f'  ok   ferronit {ferronit.__version__} + ferronit-core {core} (FerronitApp есть)')
" || fail=1

echo "==> HEALTHCHECK"
for _ in $(seq 1 10); do
    state="$(docker inspect -f '{{.State.Health.Status}}' "$NAME" 2>/dev/null || echo none)"
    [ "$state" = "healthy" ] && break
    sleep 2
done
if [ "$state" = "healthy" ]; then echo "  ok   status=healthy"; else echo "  FAIL status=$state"; fail=1; fi

if [ "$fail" -eq 0 ]; then
    echo "==> PASS"
else
    echo "==> FAIL"
fi
exit "$fail"
