# Примеры Ferronit

Каждый файл — самостоятельное приложение (`app`) без внешних сервисов: запускается
одной командой и проверяется `curl`. Это «живые» примеры под конкретные кейсы;
`app.py` рядом — полный e-commerce на DDD + CQRS + SQLite/Postgres.

| Файл | Что показывает |
|---|---|
| `minimal_app.py` | база: маршруты, типизированные path/query-параметры, JSON и текст |
| `di_app.py` | DI-контейнер: граф зависимостей по аннотациям, singleton-сервис |
| `llm_sse_app.py` | LLM как порт (OpenAI/Claude/Mock) + SSE-стриминг токенов |
| `rag_app.py` | RAG: векторный порт (Qdrant/Chroma/Mock) + LLM на Mock-адаптерах |
| `ws_app.py` | WebSocket с проверкой Origin (CSRF-over-WebSocket guard) |
| `app.py` | полный DDD-пример: агрегаты, CommandBus, UnitOfWork, SQLAlchemy |

## Запуск

```bash
cd <корень репозитория>

# Granian (прод-сервер, Rust) — модуль указывается как python-путь
.venv/bin/python -m granian --interface asgi --no-ws examples.minimal_app:app

# uvicorn
.venv/bin/python -m uvicorn examples.minimal_app:app --port 8000

# или прямо файлом (внутри вызывается uvicorn)
.venv/bin/python examples/minimal_app.py
```

## Проверка

```bash
curl "localhost:8000/hello?name=Денис"          # minimal_app
curl localhost:8000/items/42
curl -X POST localhost:8000/echo -H 'content-type: application/json' -d '{"a": 1}'

curl localhost:8000/greet/Денис                 # di_app

curl -X POST localhost:8000/chat -H 'content-type: application/json' \
     -d '{"prompt": "что такое Ferronit"}'          # llm_sse_app
curl -N -X POST localhost:8000/chat/stream -H 'content-type: application/json' \
     -d '{"prompt": "стриминг"}'

curl -X POST localhost:8000/ask -H 'content-type: application/json' \
     -d '{"question": "почему Rust в ядре"}'     # rag_app
```

Все примеры покрыты тестами: `tests/test_examples.py` (в том числе WebSocket —
через прямой ASGI-диалог без браузера).
