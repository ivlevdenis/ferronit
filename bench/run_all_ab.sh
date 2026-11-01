#!/usr/bin/env bash
# Полный прогон ab-стенда: ping + GET /rows?limit + одиночные записи.
# В одной сессии меряются 5 Python-фреймворков и Go-стенд (pgx/sqlx/GORM/ent):
# раунд «каждое приложение по одному разу», BENCH_RUNS=3, берётся лучшее.
#
#   ./bench/run_all_ab.sh                 # ~15 минут
#   BENCH_NO_GO=1 ./bench/run_all_ab.sh   # только Python-фреймворки
#   BENCH_APPS=ferronit,go-pgx ./bench/run_all_ab.sh   # выборочно
#
# Всё измеряется ApacheBench (C-клиент). Перед запуском стоит погасить фоновые
# потребители CPU (ollama, qdrant) — иначе цифры не сравнимы между сессиями.
# Go-бинарник стенда собирается автоматически, если его нет или он старше main.go.
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
