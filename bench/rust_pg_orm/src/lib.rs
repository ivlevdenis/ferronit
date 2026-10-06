//! Стенд сравнения слоёв доступа к PostgreSQL: Python против Rust.
//!
//! Бинарники:
//! * `rust_pg_bench` — прямой замер слоёв (токио-постгрес, sqlx, SeaORM) против `bench/pg_direct.py`;
//! * `http_service` — HTTP-сервис на axum с теми же маршрутами, что у Ferrox в `bench_postgres.py`,
//!   для сравнения приложений целиком.

pub mod entity;
