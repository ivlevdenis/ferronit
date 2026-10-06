# LLM в бизнес-логике Ferrox — дизайн (черновик)

> Дата: 30 авг 2026. Статус: обсуждение, не реализовано. Возможно используем позже.

## 1. Главный принцип: LLM — внешний сервис, как БД или Kafka

Бизнес-логика зависит от **интерфейса** (`LlmPort`), не от реализации:

```python
# ПРАВИЛЬНО: сервис зависит от LlmPort (DI подставит что угодно)
class OrderService:
    def __init__(self, llm: LlmPort, repo):
        self._llm = llm
```

- **Домен (AggregateRoot) не знает про LLM** — чистый домен, без внешних зависимостей
- **ApplicationService (use case)** — единственное место вызова LLM
- Тесты — через `MockLlmAdapter` (уже есть в ferrox/contrib/llm.py) — детерминированные

## 2. Особые свойства LLM

| Свойство | Следствие |
|---|---|
| Недетерминизм | вывод валидировать (structured output → pydantic), retry при мусоре |
| Медленный и хрупкий | timeout, retry, fallback; **НЕ внутри транзакции БД** (сначала LLM, потом uow) |
| Дорогой | кэш, лимиты, логирование токенов (usage уже есть в ChatResponse) |

## 3. Слои-обёртки вокруг порта

```
ApplicationService
    ↓
Structured (LLM → pydantic, retry при невалидном JSON)
    ↓
Reliable (timeout · retry · fallback модель · circuit breaker)
    ↓
LlmPort (OpenAI / Claude / Mock)
```

## 4. Паттерны по кейсам

- **Извлечение структуры** (заказ из письма): LLM → JSON → pydantic → валидация → human-in-the-loop
- **Классификация** (маршрутизация тикетов): LLM → enum → дефолт при неуверенности
- **Генерация** (черновик ответа): LLM → человек правит → отправка
- **Агентский цикл**: управляемый цикл в ApplicationService (не в домене), лимит шагов

## 5. CQRS-интеграция

```python
@app.route("/orders/from-email", methods=["POST"])
async def create_from_email(req):
    cmd = CreateOrderFromEmailCmd(await req.json())
    result = await cmd_bus.dispatch(cmd)   # → CommandBus → OrderService

# OrderService.create_from_email:
#   1. LLM-извлечение (ВНЕ транзакции)
#   2. бизнес-правила (сумма, дубликаты)
#   3. async with uow: save
```

## 6. Что Ferrox может дать как фреймворк

| Фича | Что делает |
|---|---|
| `structured(llm, model, prompt, retries=2)` | LLM → pydantic, retry на мусорный вывод |
| `ReliableLlm(port, timeout, retries, fallback)` | таймаут, экспоненциальный retry, деградация модели |
| `CachedLlm(port, ttl)` | кэш повторяющихся вызовов |
| Guardrails | prompt injection защита (user-контент не в system prompt), PII-маскировка |
| DI + Mock | `c.singleton(LlmPort, OpenAiAdapter())` — тесты через Mock |

## 7. Предложенный порядок работ (ROI)

1. `structured()` — самый частый кейс, без него LLM в проде опасен (мусорный JSON)
2. Reliable-обёртка (timeout/retry/fallback)
3. Кэш + логирование стоимости
4. Guardrails (prompt injection) — важно для user-facing
