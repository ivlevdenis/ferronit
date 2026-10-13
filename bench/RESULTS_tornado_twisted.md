# Полный замер: Ferronit против Tornado и Twisted

ab -c 50 -k (кроме DELETE — без `-k`, т.к. Tornado вешает keep-alive DELETE без тела под ab -k),
лучшее из 3, таблица bench_rows — 1000 строк, одна машина.
Tornado/Twisted — на своём сервере (не ASGI, не granian); Ferronit — granian, 1 воркер.

| показатель (req/s) | Ferronit B (granian) | Tornado (свой сервер) | Twisted (свой сервер) |
|---|---|---|---|
| GET /ping | 97 358 | 8 978 | 10 129 |
| GET /rows?limit=1 | 11 737 | 4 357 | 4 503 |
| GET /rows?limit=20 | 10 233 | 3 672 | 4 003 |
| GET /rows?limit=50 | 8 520 | 3 370 | 3 590 |
| GET /rows?limit=100 | 6 604 | 2 906 | 3 044 |
| GET /rows?limit=1000 | 1 302 | 916 | 894 |
| POST /rows (INSERT) | 2 130 | 4 545 | 3 468 |
| PUT /rows/1 (UPDATE) | 954 | 1 074 | 1 084 |
| POST /rows/upsert | 961 | 1 096 | 1 093 |
| DELETE /rows/1 | 6 392 | 4 068 | 4 554 |
