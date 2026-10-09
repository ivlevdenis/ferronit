#!/usr/bin/env bash
# Полный прогон ab-стенда: все цифры README снимаются одним запуском и пишутся в файл.
#
#   ./bench/run_all_ab.sh            # ~10 минут
#
# Всё измеряется ApacheBench (C-клиент). Перед запуском стоит погасить фоновые
# потребители CPU (ollama, qdrant) — иначе цифры не сравнимы между сессиями.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
PY=".venv/bin/python"
OUT="bench/RESULTS_ab.txt"

{
    echo "# Ferronit — замеры ab, $(date '+%Y-%m-%d %H:%M %Z')"
    echo "# машина: $(nproc) ядер, load $(cut -d' ' -f1-3 /proc/loadavg), $(uname -sr)"
    echo "# ab: $(ab -V | head -1)"
} > "$OUT"

run() {
    echo
    echo "### ab_bench.py $*"
    "$PY" bench/ab_bench.py "$@" 2>&1 | tee -a "$OUT"
}

# маршрутизация: 50 и 1000 маршрутов, оба сервера
run --server uvicorn --routes 50   --requests 20000 --runs 3
run --server granian --routes 50   --requests 30000 --runs 3
run --server uvicorn --routes 1000 --requests 10000 --runs 3
run --server granian --routes 1000 --requests 20000 --runs 3

# размер ответа
run --server uvicorn --payload small  --requests 10000 --runs 3
run --server uvicorn --payload medium --requests 5000  --runs 3
run --server uvicorn --payload big    --requests 3000  --runs 3
run --server granian --payload big    --requests 5000  --runs 3

echo
echo "готово: $OUT"
