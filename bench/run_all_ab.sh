#!/usr/bin/env bash
# Полный прогон ab-стенда: ping + GET /rows?limit + одиночные записи (5 фреймворков).
#
#   ./bench/run_all_ab.sh            # ~5–6 минут
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

echo "### api_bench.py (ping + GET /rows?limit + одиночные записи)" >> "$OUT"
"$PY" bench/api_bench.py 2>&1 | tee -a "$OUT"

echo
echo "готово: $OUT"
