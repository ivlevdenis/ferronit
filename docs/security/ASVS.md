# Ferrox — ASVS Level 1 Compliance Map

**Стандарт:** OWASP ASVS 5.0 (2025), требования уровня 1 (L1)
**Дата:** август 2026
**Всего L1-требований в стандарте:** 70 (V16 Logging и V17 WebRTC не имеют L1)

## Легенда статусов

| Статус | Значение |
|---|---|
| ✅ | Реализовано в Ferrox + есть тест |
| 🟡 | Частично / паттерн доступен, но требует настройки приложения |
| 🔴 | App-level — ответственность приложения, не фреймворка |
| N/A | Не применимо по дизайну (фреймворк не делает этого) |

---

## V1 — Encoding and Sanitization (8 L1)

| # | Требование | Статус | Где / тест |
|---|---|---|---|
| 1.2.1 | Output encoding для HTTP/HTML/XML | ✅ | JSON-escape HTML (XSS-тест + ASVS V1.2.1) |
| 1.2.2 | URL-encoding при построении URL | ✅ | Ferrox не строит URL из ввода (ASVS V1.2.2) |
| 1.2.3 | Escaping JS-контента | N/A | Ferrox не генерирует JS |
| 1.2.4 | **Параметризованные SQL-запросы** | ✅ | CoreRepository (SQLAlchemy bind params), тесты SQL-инъекций |
| 1.2.5 | Защита от OS command injection | N/A | Ferrox не исполняет команды из ввода |
| 1.3.1 | Санитизация HTML (WYSIWYG) | 🔴 | Приложение |
| 1.3.2 | Без eval()/динамического кода | ✅ | В рантайме Ferrox не использует eval (только в бенч-генераторах) |
| 1.5.1 | XML-парсеры в restrictive-режиме | N/A | Ferrox парсит только JSON (Rust serde_json) |

## V2 — Validation and Business Logic (4 L1)

| # | Требование | Статус | Где / тест |
|---|---|---|---|
| 2.1.1 | Документация правил валидации ввода | ✅ | README «Input validation rules»: типы path params, лимиты JSON/тела, кодировки |
| 2.2.1 | Валидация по бизнес-ожиданиям | 🔴 | Приложение (типы: int path params ✅) |
| 2.2.2 | Валидация на доверенном слое | 🔴 | Приложение |
| 2.3.1 | Корректные бизнес-потоки | 🔴 | Приложение |

## V3 — Web Frontend Security (8 L1)

| # | Требование | Статус | Где / тест |
|---|---|---|---|
| 3.2.1 | Anti-clickjacking (X-Frame-Options) | ✅ | `security_headers()`, тест |
| 3.2.2 | Текст не рендерится как HTML | N/A | Ferrox — JSON/plain API, HTML не рендерит (нет шаблонизатора) |
| 3.3.1 | Cookies: Secure/HttpOnly | 🔴 | Ferrox не управляет cookies (паттерн: заголовки) |
| 3.4.1 | **Strict-Transport-Security** | ✅ | `security_headers()` (max-age 31536000), тест |
| 3.4.2 | CORS ACAO — фиксированное значение | ✅ | `cors(allow_origins=[...])`, тест denied origin |
| 3.5.1 | CORS без reliance на preflight | ✅ | ACAO только для разрешённого Origin, тест |
| 3.5.2 | Preflight для чувствительных запросов | ✅ | Проверка Origin в preflight (204/deny), тест |
| 3.5.3 | Чувствительные операции — POST/PUT | 🔴 | Приложение |

## V4 — API and Web Service (2 L1)

| # | Требование | Статус | Где / тест |
|---|---|---|---|
| 4.1.1 | **Content-Type соответствует контенту + charset** | ✅ | `application/json; charset=utf-8`, `text/plain; charset=utf-8` |
| 4.4.1 | WebSocket over TLS (WSS) | 🔴 | Сервер (granian/nginx TLS); HSTS ✅ |

## V5 — File Handling (4 L1)

| # | Требование | Статус | Где / тест |
|---|---|---|---|
| 5.2.1 | **Лимит размера файлов** | ✅ | `Ferrox(max_body_size=...)` → 413, тест (в т.ч. mid-chunk) |
| 5.2.2 | Archive bomb (распаковка) | N/A | Ferrox не распаковывает архивы |
| 5.3.1 | Загруженные файлы не исполняются | ✅ | Статика отдаёт как контент (mimetypes), не исполняет (ASVS V5.3.1) |
| 5.3.2 | **Пути из untrusted-ввода** | ✅ | staticfiles: normpath + realpath (symlink) + dotfiles, тесты |

## V6 — Authentication (13 L1)

| # | Требование | Статус | Где / тест |
|---|---|---|---|
| 6.1.1 | Документация: rate limiting, anti-automation | ✅ | README «Rate limiting strategy» (login 5/min, API 100/min, sensitive 10/min) |
| 6.2.1-6.2.8 (8 шт) | Пароли: длина, смена, чек-листы, маскировка | 🔴 | Приложение |
| 6.3.1 | **Anti-bruteforce / credential stuffing** | ✅ | `rate_limit(limit, window)` → 429, тесты |
| 6.3.2 | Нет дефолтных учёток | 🔴 | Приложение |
| 6.4.1 | Случайные начальные пароли | 🔴 | Приложение |
| 6.4.2 | Без password hints | 🔴 | Приложение |

## V7 — Session Management (6 L1)

Все требования — 🔴 app-level (Ferrox не реализует сессии; паттерн — middleware/приложение).

## V8 — Authorization (4 L1)

Все — 🔴 app-level (паттерн: auth-middleware short-circuit, тест есть; роли/ACL — приложение).

## V9 — Self-contained Tokens (4 L1)

Все — 🔴 app-level (JWT и т.п. — приложение).

## V10 — OAuth and OIDC (5 L1)

Все — 🔴 app-level.

## V11 — Cryptography (3 L1)

Все — 🔴 app-level (Ferrox не шифрует данные; криптография — приложение).

## V12 — Secure Communication (3 L1)

| # | Требование | Статус | Где / тест |
|---|---|---|---|
| 12.1.1 | TLS 1.2+ | 🔴 | Сервер (granian) |
| 12.2.1 | TLS для всех соединений | 🔴 | Сервер |
| 12.2.2 | Публичные доверенные сертификаты | 🔴 | Сервер |

## V13 — Configuration (1 L1)

| # | Требование | Статус | Где / тест |
|---|---|---|---|
| 13.4.1 | .git/.svn недоступны | ✅ | dotfiles в статике → 404, тест |

## V14 — Data Protection (2 L1)

Оба — 🔴 app-level (клиентское хранилище, чувствительные данные — приложение).

## V15 — Secure Coding and Architecture (3 L1)

| # | Требование | Статус | Где / тест |
|---|---|---|---|
| 15.1.1 | Документация по 3rd-party компонентам | 🔴 | Вне фреймворка (CI/pip-audit) |
| 15.2.1 | Актуальные компоненты | 🔴 | Вне фреймворка (dependabot) |
| 15.3.1 | Возвращать только нужные поля | 🔴 | Приложение (ответы — код приложения) |

---

## Сводка по Ferrox

| Категория | ✅ | 🟡 | 🔴 | N/A |
|---|---|---|---|---|
| V1 Encoding | 4 | 0 | 1 | 3 |
| V2 Validation | 1 | 0 | 3 | 0 |
| V3 Frontend | 5 | 0 | 2 | 1 |
| V4 API | 1 | 0 | 1 | 0 |
| V5 Files | 3 | 0 | 0 | 1 |
| V6 Auth | 2 | 0 | 11 | 0 |
| V7-V12 | 0 | 0 | 25 | 0 |
| V13 Config | 1 | 0 | 0 | 0 |
| V14 Data | 0 | 0 | 2 | 0 |
| V15 Coding | 0 | 0 | 3 | 0 |
| **Итого** | **17** | **0** | **48** | **5** |

**17 из 70 требований L1 реализованы в фреймворке и покрыты тестами/документацией**
(14 тестов в `tests/security/test_asvs_l1.py`, имена вида `test_asvs_v1_2_4_...`).

## Применимые требования L1 — все закрыты

Все требования L1, применимые к веб-фреймворку на уровне инфраструктуры, реализованы
(✅) или не применимы по дизайну (N/A). Оставшиеся 🔴 — бизнес-логика приложения:
аутентификация, сессии, авторизация, OAuth, криптография, TLS-терминация.
