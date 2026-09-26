# Graph Report - velox  (2026-09-26)

## Corpus Check
- 94 files · ~33,962 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1018 nodes · 2032 edges · 69 communities (58 shown, 11 thin omitted)
- Extraction: 85% EXTRACTED · 15% INFERRED · 0% AMBIGUOUS · INFERRED: 312 edges (avg confidence: 0.54)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_app.py|app.py]]
- [[_COMMUNITY_test_infra.py|test_infra.py]]
- [[_COMMUNITY_VeloxApp|VeloxApp]]
- [[_COMMUNITY_.factory|.factory]]
- [[_COMMUNITY_Velox|Velox]]
- [[_COMMUNITY_test_serialization.py|test_serialization.py]]
- [[_COMMUNITY_test_asvs_l1.py|test_asvs_l1.py]]
- [[_COMMUNITY_MockLlmAdapter|MockLlmAdapter]]
- [[_COMMUNITY_VectorDbPort|VectorDbPort]]
- [[_COMMUNITY_test_security.py|test_security.py]]
- [[_COMMUNITY_test_security3.py|test_security3.py]]
- [[_COMMUNITY_test_security5.py|test_security5.py]]
- [[_COMMUNITY_CoreRepository|CoreRepository]]
- [[_COMMUNITY_Velox — ASVS Level 1 Compliance Map|Velox — ASVS Level 1 Compliance Map]]
- [[_COMMUNITY_WebSocket|WebSocket]]
- [[_COMMUNITY_Cache|Cache]]
- [[_COMMUNITY_bench_full.py|bench_full.py]]
- [[_COMMUNITY_Response|Response]]
- [[_COMMUNITY_test_security2.py|test_security2.py]]
- [[_COMMUNITY_app.py|app.py]]
- [[_COMMUNITY_OpenAPI|OpenAPI]]
- [[_COMMUNITY_RelationalUnitOfWork|RelationalUnitOfWork]]
- [[_COMMUNITY_request.py|request.py]]
- [[_COMMUNITY_Request|Request]]
- [[_COMMUNITY_KafkaMessageBus|KafkaMessageBus]]
- [[_COMMUNITY__Node|_Node]]
- [[_COMMUNITY_prof_components.py|prof_components.py]]
- [[_COMMUNITY_Benchmarks|Benchmarks]]
- [[_COMMUNITY_prof_clean.py|prof_clean.py]]
- [[_COMMUNITY_test_db_e2e.py|test_db_e2e.py]]
- [[_COMMUNITY_test_postgres_real.py|test_postgres_real.py]]
- [[_COMMUNITY_cli.py|cli.py]]
- [[_COMMUNITY_bench_db.py|bench_db.py]]
- [[_COMMUNITY_LLM в бизнес-логике Velox — дизайн (черновик)|LLM в бизнес-логике Velox — дизайн (черновик)]]
- [[_COMMUNITY_Middleware|Middleware]]
- [[_COMMUNITY_test_db_core.py|test_db_core.py]]
- [[_COMMUNITY_test_routing.py|test_routing.py]]
- [[_COMMUNITY_JSONResponse|JSONResponse]]
- [[_COMMUNITY_bench_postgres.py|bench_postgres.py]]
- [[_COMMUNITY_Velox — стратегия применения (заметки)|Velox — стратегия применения (заметки)]]
- [[_COMMUNITY_test_app.py|test_app.py]]
- [[_COMMUNITY_test_injection.py|test_injection.py]]
- [[_COMMUNITY_HealthCheck|HealthCheck]]
- [[_COMMUNITY_bench_db_pool.py|bench_db_pool.py]]
- [[_COMMUNITY_bench_db_variants.py|bench_db_variants.py]]
- [[_COMMUNITY_bench_network.py|bench_network.py]]
- [[_COMMUNITY_bench_payload.py|bench_payload.py]]
- [[_COMMUNITY_fastapi_app.py|fastapi_app.py]]
- [[_COMMUNITY_test_bench.py|test_bench.py]]
- [[_COMMUNITY_bench_real.py|bench_real.py]]
- [[_COMMUNITY_micro_bench_db.py|micro_bench_db.py]]
- [[_COMMUNITY_test_postgres.py|test_postgres.py]]
- [[_COMMUNITY_test_ws.py|test_ws.py]]
- [[_COMMUNITY_test_middleware.py|test_middleware.py]]
- [[_COMMUNITY_cors.py|cors.py]]
- [[_COMMUNITY_db.py|db.py]]
- [[_COMMUNITY_micro_bench_json.py|micro_bench_json.py]]
- [[_COMMUNITY_bench_modes.py|bench_modes.py]]
- [[_COMMUNITY___init__.py|__init__.py]]
- [[_COMMUNITY_velox|velox]]

## God Nodes (most connected - your core abstractions)
1. `Velox` - 135 edges
2. `CommandBus` - 40 edges
3. `AggregateRoot` - 37 edges
4. `Command` - 36 edges
5. `StaticFiles` - 30 edges
6. `QueryBus` - 30 edges
7. `RelationalUnitOfWork` - 29 edges
8. `ApplicationService` - 28 edges
9. `Response` - 27 edges
10. `Query` - 27 edges

## Surprising Connections (you probably didn't know these)
- `app()` --references--> `Velox`  [EXTRACTED]
  tests/test_app.py → velox/core/app.py
- `app()` --calls--> `Velox`  [EXTRACTED]
  tests/test_bench.py → velox/core/app.py
- `OrderModel` --uses--> `RelationalUnitOfWork`  [INFERRED]
  tests/test_db_e2e.py → velox/contrib/db.py
- `app()` --calls--> `Velox`  [EXTRACTED]
  tests/test_injection.py → velox/core/app.py
- `app()` --calls--> `Velox`  [EXTRACTED]
  tests/test_middleware.py → velox/core/app.py

## Import Cycles
- None detected.

## Communities (69 total, 11 thin omitted)

### Community 0 - "app.py"
Cohesion: 0.05
Nodes (75): ABC, Base, BaseModel, add_item(), AddToCartCmd, Cart, CartItem, CartItemModel (+67 more)

### Community 1 - "test_infra.py"
Cohesion: 0.06
Nodes (28): Config + DI + Tracing + LLM tests., test_di_missing_raises(), test_di_resolve_with_defaults(), test_di_singleton_and_factory(), test_env_config_defaults(), test_env_config_prefix(), test_env_config_typed(), test_trace_id_generated() (+20 more)

### Community 2 - "VeloxApp"
Cohesion: 0.13
Nodes (24): Bound, HashMap, MatchitRouter, Option, Py, PyAny, PyBytes, PyModule (+16 more)

### Community 3 - ".factory"
Cohesion: 0.09
Nodes (22): Base, create_user(), list_users(), User, Base, create_user(), list_users(), create_user() (+14 more)

### Community 4 - "Velox"
Cohesion: 0.09
Nodes (20): call_app(), Security tests — fourth batch: content negotiation, trace spoofing, header flood, Percent-encoded UTF-8 декодируется в нормальную строку, не в кракозябры., Middleware-паттерн авторизации: без токена — 401, хендлер не вызывается., accept-encoding: gzip;q=0 — клиент не хочет gzip, не сжимаем., Пользовательский X-Trace-Id не попадает в ответ без trace middleware., test_auth_middleware_short_circuit(), test_broken_query_pairs_no_crash() (+12 more)

### Community 5 - "test_serialization.py"
Cohesion: 0.10
Nodes (22): Protocol, app(), Item, Serialization tests — Pydantic codec integration., Codec, decode_json(), encode_json(), get_codec() (+14 more)

### Community 6 - "test_asvs_l1.py"
Cohesion: 0.09
Nodes (30): call_app(), ASVS 5.0 Level 1 compliance tests — каждый тест = одно требование L1.  Имена: te, V3.2.1 (L1): security controls prevent browsers from rendering content     in a, V3.4.1 (L1): Strict-Transport-Security header is included on all responses., V3.4.2 (L1): CORS Access-Control-Allow-Origin is a fixed value and does     not, V3.5.2 (L1): if the application relies on the CORS preflight mechanism,     disa, V4.1.1 (L1): every HTTP response with a message body contains a     Content-Type, V5.2.1 (L1): the application accepts only files of a size it can process     (ma (+22 more)

### Community 7 - "MockLlmAdapter"
Cohesion: 0.11
Nodes (13): test_mock_llm_chat(), test_mock_llm_embed(), ChatMessage, ChatResponse, ClaudeAdapter, LlmPort, MockLlmAdapter, OpenAiAdapter (+5 more)

### Community 8 - "VectorDbPort"
Cohesion: 0.09
Nodes (10): ChromaAdapter, _cosine(), MockVectorDb, QdrantAdapter, Vector Database port + adapters — Qdrant, Chroma, Mock., Semantic search port — upsert, search, delete embeddings., In-memory vector DB for testing — brute-force cosine similarity., Qdrant vector database adapter. (+2 more)

### Community 9 - "test_security.py"
Cohesion: 0.11
Nodes (24): call_app(), make_db_uow(), Security tests — first 10: injection, traversal, header injection, CORS, error h, Заголовок с CRLF из пользовательского ввода не создаёт новые заголовки., JSON-ответ не должен содержать сырой <script> (XSS при <script>JSON</script>)., Глубокая вложенность JSON не роняет процесс — валидный HTTP-ответ., Origin вне списка не получает Access-Control-Allow-Origin., debug=False: 500 без деталей исключения. (+16 more)

### Community 10 - "test_security3.py"
Cohesion: 0.16
Nodes (21): call_app(), make_json_app(), Security tests — third batch: JSON parsing attacks, path param decoding, middlew, Бинарный мусор вместо JSON — 400, не 500., json() без тела — 400 (не 500, не краш)., Даже с text/plain заголовком битый body → 400., ASGI path уже декодирован — параметр приходит как есть, без двойного декода., Ошибка в middleware — 500 без внутренностей. (+13 more)

### Community 11 - "test_security5.py"
Cohesion: 0.16
Nodes (19): call_app(), Security tests — production hardening batch: security headers, WS origin, body s, Лимит срабатывает во время чанкованной загрузки, не после., TRACE/TRACK — 404, не эхо (защита от TRACE-атак)., test_max_body_size_413(), test_max_body_size_chunked(), test_no_server_fingerprint_headers(), test_rate_limit_429() (+11 more)

### Community 12 - "CoreRepository"
Cohesion: 0.13
Nodes (7): AsyncSession, CoreRepository, UPDATE by primary key; returns merged dict., Repository backed by SQLAlchemy async session., SQLAlchemy Core repository — rows in, dicts out (no ORM mapping).      Usage:, INSERT; returns data + generated PK.          With `returning=True` the full row, RelationalRepository

### Community 13 - "Velox — ASVS Level 1 Compliance Map"
Cohesion: 0.10
Nodes (19): V10 — OAuth and OIDC (5 L1), V11 — Cryptography (3 L1), V12 — Secure Communication (3 L1), V13 — Configuration (1 L1), V14 — Data Protection (2 L1), V15 — Secure Coding and Architecture (3 L1), V1 — Encoding and Sanitization (8 L1), V2 — Validation and Business Logic (4 L1) (+11 more)

### Community 14 - "WebSocket"
Cohesion: 0.13
Nodes (10): Enum, Velox — High-performance ASGI web framework with DDD support., WebSocket support — standards-compliant ASGI WebSocket handling., ASGI WebSocket connection — receive/send text, bytes, JSON., Accept the WebSocket connection., Receive one WebSocket message (str for text, bytes for binary)., Receive and parse one JSON message., Close the WebSocket connection. (+2 more)

### Community 15 - "Cache"
Cohesion: 0.11
Nodes (7): Cache, EventBus, InMemoryCache, InMemoryEventBus, Any, Key-value cache port (Redis, Memcached, in-memory)., In-process domain event bus — publish/subscribe within the same process.

### Community 16 - "bench_full.py"
Cohesion: 0.24
Nodes (17): asgi_bench(), bench_asgi(), bench_http(), fmt_gain(), main(), make_app_code(), make_velox(), network_bench() (+9 more)

### Community 17 - "Response"
Cohesion: 0.16
Nodes (12): main(), noop_send(), Микро-разбор Response._send: что именно стоит 3.86 мкс., Security headers — production defaults in one middleware.  Usage:     from velox, Request, Response, Static files serving — directory mount with caching and range support., _serve_file() (+4 more)

### Community 18 - "test_security2.py"
Cohesion: 0.19
Nodes (16): call_app(), Security tests — second batch: static symlink/dotfiles, header injection, WebSoc, CRLF в content_type ответа не создаёт новые заголовки., \\x00 и \\x1f в значениях не попадают в ответ сырыми байтами., Symlink внутри статики, указывающий наружу, не отдаёт внешние файлы., .env/.git не должны отдаваться из статики., test_content_type_header_injection(), test_control_chars_escaped_in_json() (+8 more)

### Community 19 - "app.py"
Cohesion: 0.16
Nodes (11): _lifespan(), Velox app — Rust routing, Python handler execution., Парсинг Accept-Encoding с учётом q-факторов (gzip;q=0 → False)., Поиск заголовка в scope до создания Request (для CORS)., _scope_header(), _send_empty(), _wants_gzip(), _clean_header() (+3 more)

### Community 20 - "OpenAPI"
Cohesion: 0.17
Nodes (7): _get_return_type(), OpenAPI, Any, OpenAPI 3.0 schema generator — auto-detects types from signatures., Build OpenAPI schema objects from Pydantic models., OpenAPI 3.0 builder with auto type introspection., SchemaBuilder

### Community 21 - "RelationalUnitOfWork"
Cohesion: 0.20
Nodes (10): async_sessionmaker, V1.2.4 (L1): data selection or database queries use parameterized queries., test_asvs_v1_2_4_parameterized_queries(), uow(), uow(), uow(), create_relational_uow(), Unit of Work — auto-commit on exit.      Usage:         async with uow: (+2 more)

### Community 22 - "request.py"
Cohesion: 0.15
Nodes (12): Exception, BodyTooLarge, Request object — Rust-native parsing for headers, query, JSON., Клиентская ошибка запроса (invalid body/JSON) → HTTP 400., Тело запроса превышает max_body_size → HTTP 413., RequestError, _cast(), inject() (+4 more)

### Community 23 - "Request"
Cohesion: 0.18
Nodes (3): ASGI request — delegates parsing to Rust where possible., Быстрый поиск одного заголовка без полного парсинга (Rust)., Request

### Community 24 - "KafkaMessageBus"
Cohesion: 0.18
Nodes (5): KafkaMessageBus, Kafka adapter — aiokafka-based MessageBus implementation., Async Kafka message bus via aiokafka.      Usage:         bus = KafkaMessageBus(, MessageBus, External message bus port (Kafka, RabbitMQ, PubSub).

### Community 25 - "_Node"
Cohesion: 0.26
Nodes (8): _find_child(), _find_child_by_param(), _Node, Radix tree router — O(path_length) lookup, no regex overhead., Compiled route table — radix tree per method type., Register a route handler for a method + path pattern., Find handler and path parameters. Returns (handler, params)., Router

### Community 26 - "prof_components.py"
Cohesion: 0.26
Nodes (10): bench(), main(), noop_receive(), noop_send(), Разложение горячего пути Velox на компоненты (мкс/оп)., current_trace_id(), Distributed tracing — X-Trace-Id header injection and propagation., Get current trace ID from context. (+2 more)

### Community 27 - "Benchmarks"
Cohesion: 0.17
Nodes (11): Benchmarks, Payload size (uvicorn, 3 routes, median of 3 runs), PostgreSQL 18 (Docker, default settings, Granian, ab), Raw client (ApacheBench, 50 concurrent connections, keep-alive), Reproduce, Run, Security (production hardening), SQLite scaling (what actually works) (+3 more)

### Community 28 - "prof_clean.py"
Cohesion: 0.25
Nodes (8): measure(), Честный замер hot path: gc выключен, медиана из 3 прогонов., recv(), run(), send(), make_scope(), Чистый профиль Velox: прямой вызов ASGI, без httpx-шума., run()

### Community 29 - "test_db_e2e.py"
Cohesion: 0.22
Nodes (7): client(), make_app(), E2E: Velox HTTP → handler → database (SQLite) → response.  Полный путь запроса:, Настоящий SQL-запрос напрямую к БД после HTTP-записи., Ошибка в хендлере → транзакция откатывается, в БД пусто., test_raw_sql_query(), test_rollback_on_error()

### Community 30 - "test_postgres_real.py"
Cohesion: 0.20
Nodes (8): client(), make_app(), E2E: Velox HTTP → CoreRepository → реальный PostgreSQL (Docker, дефолтные настро, Настоящий SQL к Postgres после HTTP-записи., RETURNING возвращает server_defaults (на PG INSERT...RETURNING нативный)., test_pg_raw_sql(), test_pg_returning_defaults(), uow()

### Community 31 - "cli.py"
Cohesion: 0.40
Nodes (9): Namespace, _check_granian(), cmd_dev(), cmd_new(), cmd_run(), _find_app(), main(), Velox CLI — project scaffolding and dev server. (+1 more)

### Community 32 - "bench_db.py"
Cohesion: 0.33
Nodes (8): ab_bench(), bench_pair(), main(), Popen, Бенч с БД: Velox vs FastAPI, HTTP → handler → SQLite → response.  GET /users — S, Прогон через ab, возвращает req/s., start_server(), wait_ready()

### Community 33 - "LLM в бизнес-логике Velox — дизайн (черновик)"
Cohesion: 0.22
Nodes (8): 1. Главный принцип: LLM — внешний сервис, как БД или Kafka, 2. Особые свойства LLM, 3. Слои-обёртки вокруг порта, 4. Паттерны по кейсам, 5. CQRS-интеграция, 6. Что Velox может дать как фреймворк, 7. Предложенный порядок работ (ROI), LLM в бизнес-логике Velox — дизайн (черновик)

### Community 34 - "Middleware"
Cohesion: 0.22
Nodes (5): Handler, MiddlewareFunc, Middleware, Stack of middleware functions., Wrap handler with middleware stack.

### Community 35 - "test_db_core.py"
Cohesion: 0.25
Nodes (5): client(), make_app(), E2E: Velox HTTP → handler → SQLAlchemy Core repository → SQLite.  Тот же путь, ч, Настоящий SQL после HTTP-записи через Core., test_raw_sql_after_http()

### Community 37 - "JSONResponse"
Cohesion: 0.22
Nodes (5): JSONResponse, Encode Pydantic/dataclass model to JSON response., Server-Sent Events / chunked streaming response., JSON response with automatic serialisation., StreamingResponse

### Community 38 - "bench_postgres.py"
Cohesion: 0.39
Nodes (7): ab_bench(), bench_pair(), main(), Popen, Бенч с реальным PostgreSQL (Docker): Velox (Core/ORM) vs FastAPI.  Требует: dock, start_server(), wait_ready()

### Community 39 - "Velox — стратегия применения (заметки)"
Cohesion: 0.25
Nodes (7): Velox — стратегия применения (заметки), Архитектурные роли, Вывод, Где Velox силён, Где НЕ применять, Стратегии внедрения, Что допилить для лёгкого применения

### Community 42 - "HealthCheck"
Cohesion: 0.29
Nodes (3): HealthCheck, Collect health status from registered checkers.      Usage:         hc = HealthC, Register a check: sync or async callable returning bool or HealthStatus.

### Community 43 - "bench_db_pool.py"
Cohesion: 0.43
Nodes (6): ab_bench(), main(), Popen, Эксперимент 2: пул коннектов к ФАЙЛОВОЙ SQLite (in-memory = 1 коннект = сериализ, start_server(), wait_ready()

### Community 44 - "bench_db_variants.py"
Cohesion: 0.43
Nodes (6): ab_bench(), main(), Popen, Эксперимент: варианты БД-доступа в HTTP-хендлерах (granian, ab).  1. sqlalchemy-, start_server(), wait_ready()

### Community 45 - "bench_network.py"
Cohesion: 0.43
Nodes (6): bench(), main(), Popen, Сетевой бенчмарк: Velox vs FastAPI через uvicorn, 1000 маршрутов., start_server(), wait_ready()

### Community 46 - "bench_payload.py"
Cohesion: 0.43
Nodes (6): bench(), main(), Popen, Сетевой бенч: Velox vs FastAPI с разным размером ответа (small/medium/big)., start_server(), wait_ready()

### Community 47 - "fastapi_app.py"
Cohesion: 0.33
Nodes (5): hello(), HelloReply, Request, FastAPI equivalent for comparison., reflect()

### Community 48 - "test_bench.py"
Cohesion: 0.38
Nodes (6): app(), _make_runner(), Benchmark: Velox vs FastAPI vs raw ASGI.  Важно: pytest-benchmark НЕ умеет await, Возвращает синхронную функцию: batch реальных запросов на вызов., test_get(), test_reflect()

### Community 49 - "bench_real.py"
Cohesion: 0.53
Nodes (5): bench(), main(), make_velox(), AsyncClient, Честный async-бенчмарк Velox (и FastAPI, если установлен).

### Community 50 - "micro_bench_db.py"
Cohesion: 0.47
Nodes (5): Connection, bench(), main(), make_sync_db(), Чистые замеры SQLite без прослоек: сколько выдаёт сама БД.  SELECT — 100 строк (

### Community 51 - "test_postgres.py"
Cohesion: 0.53
Nodes (5): Postgres adapter tests — using SQLite for portability., test_postgres_repo_list(), test_postgres_repo_save_and_get(), test_postgres_uow_rollback(), _UserModel

## Knowledge Gaps
- **41 isolated node(s):** `velox`, `Raw client (ApacheBench, 50 concurrent connections, keep-alive)`, `With database (SQLite, handler → ORM/Core → response, ab)`, `SQLite scaling (what actually works)`, `PostgreSQL 18 (Docker, default settings, Granian, ab)` (+36 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **11 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Velox` connect `Velox` to `app.py`, `test_infra.py`, `.factory`, `test_serialization.py`, `test_asvs_l1.py`, `test_security.py`, `test_security3.py`, `test_security5.py`, `WebSocket`, `bench_full.py`, `Response`, `test_security2.py`, `app.py`, `OpenAPI`, `request.py`, `Request`, `prof_components.py`, `prof_clean.py`, `test_db_e2e.py`, `test_postgres_real.py`, `Middleware`, `test_db_core.py`, `test_routing.py`, `JSONResponse`, `test_app.py`, `test_injection.py`, `test_bench.py`, `bench_real.py`, `test_ws.py`, `test_middleware.py`, `cors.py`, `db.py`, `_bench_full_velox.py`, `_bench_payload_velox.py`, `_bench_pool_aiosqlite-1.py`, `_bench_pool_aiosqlite-8.py`, `_bench_pool_sqlite3-8.py`?**
  _High betweenness centrality (0.399) - this node is a cross-community bridge._
- **Why does `UnitOfWork` connect `app.py` to `CoreRepository`, `RelationalUnitOfWork`, `db.py`?**
  _High betweenness centrality (0.035) - this node is a cross-community bridge._
- **Why does `Request` connect `Request` to `app.py`, `test_infra.py`, `Middleware`, `Velox`, `WebSocket`, `Response`, `app.py`, `request.py`, `prof_components.py`?**
  _High betweenness centrality (0.033) - this node is a cross-community bridge._
- **Are the 10 inferred relationships involving `Velox` (e.g. with `BodyTooLarge` and `Request`) actually correct?**
  _`Velox` has 10 INFERRED edges - model-reasoned connections that need verification._
- **Are the 29 inferred relationships involving `CommandBus` (e.g. with `AddToCartCmd` and `Cart`) actually correct?**
  _`CommandBus` has 29 INFERRED edges - model-reasoned connections that need verification._
- **Are the 29 inferred relationships involving `AggregateRoot` (e.g. with `AddToCartCmd` and `Cart`) actually correct?**
  _`AggregateRoot` has 29 INFERRED edges - model-reasoned connections that need verification._
- **Are the 29 inferred relationships involving `Command` (e.g. with `AddToCartCmd` and `Cart`) actually correct?**
  _`Command` has 29 INFERRED edges - model-reasoned connections that need verification._