#!/usr/bin/env bash
# Ferronit image smoke test: start the container, check the live app, shut it down.
#
#   ./scripts/docker_smoke.sh                 # image ferronit:0.24.0
#   IMAGE=ferronit:dev PORT=9001 ./scripts/docker_smoke.sh
set -euo pipefail

IMAGE="${IMAGE:-ferronit:0.24.0}"
PORT="${PORT:-8931}"
NAME="ferronit-smoke"

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "==> starting $IMAGE on port $PORT"
cleanup
docker run -d --rm --name "$NAME" -p "$PORT:8000" "$IMAGE" >/dev/null

echo "==> waiting for readiness (up to 30 s)"
for _ in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:$PORT/" >/dev/null 2>&1; then break; fi
    sleep 1
done

fail=0
check() { # check <name> <expected-substring> <url> [curl-args...]
    local name="$1" expect="$2" url="$3"; shift 3
    local body
    body="$(curl -sf "$@" "$url" || true)"
    if printf '%s' "$body" | grep -q "$expect"; then
        echo "  ok   $name"
    else
        echo "  FAIL $name — expected '$expect', got: ${body:0:200}"
        fail=1
    fi
}

echo "==> HTTP checks"
check "GET / (JSON + status)" '"status":"ok"'      "http://127.0.0.1:$PORT/"
check "GET / (version)"       '"version":"0.24.0"'  "http://127.0.0.1:$PORT/"
check "GET /json?n=3"         '"count":3'          "http://127.0.0.1:$PORT/json?n=3"
check "POST /echo"            '"received"'         "http://127.0.0.1:$PORT/echo" \
      -X POST -H 'content-type: application/json' -d '{"hello":"world"}'
check "GET /health"           '"status":"ok"'      "http://127.0.0.1:$PORT/health"

echo "==> checking installed distributions in the image"
docker exec "$NAME" python -c "
import importlib.metadata as m
import ferronit, ferronit_core

core = m.version('ferronit-core')
assert ferronit.__version__ == '0.24.0', ferronit.__version__
assert core == ferronit.__version__, (core, ferronit.__version__)
assert hasattr(ferronit_core, 'FerronitApp')
print(f'  ok   ferronit {ferronit.__version__} + ferronit-core {core} (FerronitApp present)')
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
