//! Общее ядро Rust-стенда: пул `sqlx`, запросы к `bench_rows` и структуры ответа.
//!
//! Контракт совпадает с `bench/api_bench.py` и `bench/go_pg_orm`: таблица `bench_rows`
//! из шести колонок (`id, name, email, created_at timestamptz, birthday date, ttl interval`),
//! даты в теле запроса не передаются — insert/upsert подставляют фиксированные значения.
//!
//! Слой доступа специально один и тот же (`sqlx` + Postgres) у обоих HTTP-сервисов:
//! стенд меряет цену фреймворка, а не ORM. Сравнение слоёв — в `bench/rust_pg_orm`.

use std::sync::atomic::{AtomicI64, Ordering};

use chrono::{DateTime, NaiveDate, TimeZone, Utc};
use serde::Serialize;
use sqlx::postgres::types::PgInterval;
use sqlx::postgres::{PgPool, PgPoolOptions};
use sqlx::Row;

/// DSN того же инстанса, что у остальных стендов.
pub const DSN: &str = "postgres://postgres:postgres@127.0.0.1:5432/postgres";

/// Лимит строк по умолчанию для `GET /rows` без query-параметра.
pub const DEFAULT_LIMIT: i64 = 100;

/// `interval` в микросекундах: в Python это `timedelta`, в JSON — секунды (как у msgspec).
#[derive(Debug, Clone, Copy)]
pub struct Interval {
    /// Длительность в микросекундах (месяцы приведены к 30 дням, как в postgres-types).
    pub micros: i64,
}

impl From<PgInterval> for Interval {
    fn from(v: PgInterval) -> Self {
        let micros = v.microseconds
            + i64::from(v.days) * 86_400_000_000
            + i64::from(v.months) * 30 * 86_400_000_000;
        Interval { micros }
    }
}

impl Serialize for Interval {
    fn serialize<S: serde::Serializer>(&self, ser: S) -> Result<S::Ok, S::Error> {
        ser.serialize_f64(self.micros as f64 / 1e6)
    }
}

/// Строка `bench_rows` в том же виде, что отдают остальные стенды.
#[derive(Debug, Clone, Serialize)]
pub struct RowOut {
    /// Первичный ключ.
    pub id: i32,
    /// Имя.
    pub name: String,
    /// Почта.
    pub email: String,
    /// `timestamptz` в ISO-8601 (msgspec и `chrono` сериализуют его одинаково).
    pub created_at: DateTime<Utc>,
    /// `date` в виде `1990-01-15`.
    pub birthday: NaiveDate,
    /// `interval`, в JSON — секунды.
    pub ttl: Interval,
}

/// Фиксированный `created_at` — то же значение, что `CREATED_AT` в `api_bench.py`.
pub fn fix_created() -> DateTime<Utc> {
    Utc.with_ymd_and_hms(2026, 1, 15, 12, 0, 0).unwrap()
}

/// Фиксированный `birthday` — то же значение, что `BIRTHDAY` в `api_bench.py`.
pub fn fix_birthday() -> NaiveDate {
    NaiveDate::from_ymd_opt(1990, 1, 15).unwrap()
}

/// Фиксированный `ttl` — то же значение, что `TTL` в `api_bench.py` (один час).
pub fn fix_ttl() -> PgInterval {
    PgInterval {
        months: 0,
        days: 0,
        microseconds: 3_600_000_000,
    }
}

/// Открывает пул соединений (`sqlx`) с заданным размером.
pub async fn connect(max_connections: u32) -> PgPool {
    PgPoolOptions::new()
        .max_connections(max_connections)
        .connect(DSN)
        .await
        .expect("пул sqlx")
}

/// `SELECT … LIMIT $1` — строки в том же порядке и того же состава, что у других стендов.
pub async fn list(pool: &PgPool, limit: i64) -> Result<Vec<RowOut>, sqlx::Error> {
    let rows = sqlx::query(
        "SELECT id, name, email, created_at, birthday, ttl FROM bench_rows LIMIT $1",
    )
    .bind(limit)
    .fetch_all(pool)
    .await?;

    Ok(rows
        .into_iter()
        .map(|row| RowOut {
            id: row.get("id"),
            name: row.get("name"),
            email: row.get("email"),
            created_at: row.get("created_at"),
            birthday: row.get("birthday"),
            ttl: Interval::from(row.get::<PgInterval, _>("ttl")),
        })
        .collect())
}

/// Вставка одной строки с фиксированными датами; возвращает новый `id`.
pub async fn insert(pool: &PgPool, name: &str, email: &str) -> Result<i32, sqlx::Error> {
    let row = sqlx::query(
        "INSERT INTO bench_rows (name, email, created_at, birthday, ttl) \
         VALUES ($1, $2, $3, $4, $5) RETURNING id",
    )
    .bind(name)
    .bind(email)
    .bind(fix_created())
    .bind(fix_birthday())
    .bind(fix_ttl())
    .fetch_one(pool)
    .await?;
    Ok(row.get("id"))
}

/// Обновление `name`/`email` по `id`; `None`, если строки нет.
pub async fn update(
    pool: &PgPool,
    id: i32,
    name: &str,
    email: &str,
) -> Result<Option<i32>, sqlx::Error> {
    let row = sqlx::query("UPDATE bench_rows SET name = $1, email = $2 WHERE id = $3 RETURNING id")
        .bind(name)
        .bind(email)
        .bind(id)
        .fetch_optional(pool)
        .await?;
    Ok(row.map(|r| r.get("id")))
}

/// `INSERT … ON CONFLICT (id) DO UPDATE` — как в остальных стендах.
pub async fn upsert(pool: &PgPool, id: i32, name: &str, email: &str) -> Result<i32, sqlx::Error> {
    let row = sqlx::query(
        "INSERT INTO bench_rows (id, name, email, created_at, birthday, ttl) \
         VALUES ($1, $2, $3, $4, $5, $6) \
         ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, email = EXCLUDED.email, \
         created_at = EXCLUDED.created_at, birthday = EXCLUDED.birthday, ttl = EXCLUDED.ttl \
         RETURNING id",
    )
    .bind(id)
    .bind(name)
    .bind(email)
    .bind(fix_created())
    .bind(fix_birthday())
    .bind(fix_ttl())
    .fetch_one(pool)
    .await?;
    Ok(row.get("id"))
}

/// Удаление строки с минимальным `id` — реальное удаление для замера: `ab` бьёт один URL,
/// поэтому `delete(id)` после первого запроса стал бы no-op.
pub async fn delete_oldest(pool: &PgPool) -> Result<(), sqlx::Error> {
    sqlx::query(
        "DELETE FROM bench_rows WHERE id = (SELECT id FROM bench_rows \
         ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED)",
    )
        .execute(pool)
        .await?;
    Ok(())
}

/// Удаление по `id`.
pub async fn delete(pool: &PgPool, id: i32) -> Result<(), sqlx::Error> {
    sqlx::query("DELETE FROM bench_rows WHERE id = $1")
        .bind(id)
        .execute(pool)
        .await?;
    Ok(())
}

/// «Разброс» по строкам: `ab` всегда шлёт один и тот же body, поэтому id выбирает
/// сервер (1..=SPREAD_ROWS), и конкурентные клиенты не сериализуются на одной строке.
pub static SPREAD_SEQ: AtomicI64 = AtomicI64::new(0);

/// Сколько строк участвует в разбросе (столько же сеяет стенд).
pub const SPREAD_ROWS: i64 = 1000;

/// Следующий id для «размазанной» записи.
pub fn next_spread_id() -> i32 {
    (SPREAD_SEQ.fetch_add(1, Ordering::Relaxed) % SPREAD_ROWS) as i32 + 1
}

/// Порт из аргументов командной строки (`--port N`), по умолчанию — `default`.
pub fn port_from_args(default: u16) -> u16 {
    let args: Vec<String> = std::env::args().collect();
    args.iter()
        .position(|a| a == "--port")
        .and_then(|i| args.get(i + 1))
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}

/// Число воркеров из аргументов командной строки (`--workers N`) — у actix это потоки.
pub fn workers_from_args(default: usize) -> usize {
    let args: Vec<String> = std::env::args().collect();
    args.iter()
        .position(|a| a == "--workers")
        .and_then(|i| args.get(i + 1))
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}

/// Размер пула из аргументов командной строки (`--pool N`).
pub fn pool_from_args(default: u32) -> u32 {
    let args: Vec<String> = std::env::args().collect();
    args.iter()
        .position(|a| a == "--pool")
        .and_then(|i| args.get(i + 1))
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}
