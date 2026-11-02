//! HTTP-стенд на **Axum** — те же маршруты и та же таблица, что у Python/Go-стендов.
//!
//! Запуск: `target/release/axum_srv --port 8261 --pool 16`
//! Один воркер эмулируется `TOKIO_WORKER_THREADS=1` (аналог `GOMAXPROCS=1` у Go).

use axum::extract::{FromRequestParts, Path, Query, State};
use axum::http::request::Parts;
use axum::http::StatusCode;
use axum::routing::{get, post, put};
use axum::{Json, Router};
use serde::{Deserialize, Serialize};
use sqlx::PgPool;

use rust_http_bench::{verify_jwt, JwtClaims, DEFAULT_LIMIT, RowOut};

#[derive(Debug, Deserialize)]
struct LimitQuery {
    limit: Option<i64>,
}

#[derive(Debug, Deserialize)]
struct NewRowBody {
    name: String,
    email: String,
}

#[derive(Debug, Deserialize)]
struct UpsertBody {
    id: i32,
    name: String,
    email: String,
}

#[derive(Debug, Serialize)]
struct RowsResponse {
    rows: Vec<RowOut>,
}

#[derive(Debug, Serialize)]
struct IdResponse {
    id: Option<i32>,
}

#[derive(Debug, Serialize)]
struct OkResponse {
    ok: bool,
}

async fn ping() -> Json<OkResponse> {
    Json(OkResponse { ok: true })
}

// ── JWT-аутентификация (стадия `jwt`) ────────────────────────────────
// Экстрактор — идиоматичный для Axum механизм; заголовок проверяется до
// хендлера, claims приходят аргументом.

#[derive(Debug, Serialize)]
struct MeResponse {
    sub: String,
    role: String,
}

#[derive(Debug, Serialize)]
struct Unauthorized {
    error: &'static str,
}

struct AuthClaim(JwtClaims);

impl<S: Send + Sync> FromRequestParts<S> for AuthClaim {
    type Rejection = (StatusCode, Json<Unauthorized>);

    async fn from_request_parts(parts: &mut Parts, _state: &S) -> Result<Self, Self::Rejection> {
        let header = parts
            .headers
            .get(axum::http::header::AUTHORIZATION)
            .and_then(|value| value.to_str().ok());
        verify_jwt(header)
            .map(AuthClaim)
            .ok_or((StatusCode::UNAUTHORIZED, Json(Unauthorized { error: "unauthorized" })))
    }
}

async fn me(AuthClaim(claims): AuthClaim) -> Json<MeResponse> {
    Json(MeResponse { sub: claims.sub, role: claims.role })
}

async fn list_rows(
    State(pool): State<PgPool>,
    Query(q): Query<LimitQuery>,
) -> Result<Json<RowsResponse>, (StatusCode, String)> {
    let limit = q.limit.unwrap_or(DEFAULT_LIMIT);
    let rows = rust_http_bench::list(&pool, limit)
        .await
        .map_err(internal)?;
    Ok(Json(RowsResponse { rows }))
}

async fn insert_row(
    State(pool): State<PgPool>,
    Json(body): Json<NewRowBody>,
) -> Result<Json<IdResponse>, (StatusCode, String)> {
    let id = rust_http_bench::insert(&pool, &body.name, &body.email)
        .await
        .map_err(internal)?;
    Ok(Json(IdResponse { id: Some(id) }))
}

async fn update_row(
    State(pool): State<PgPool>,
    Path(id): Path<i32>,
    Json(body): Json<NewRowBody>,
) -> Result<Json<IdResponse>, (StatusCode, String)> {
    let out = rust_http_bench::update(&pool, id, &body.name, &body.email)
        .await
        .map_err(internal)?;
    Ok(Json(IdResponse { id: out }))
}

async fn upsert_row(
    State(pool): State<PgPool>,
    Json(body): Json<UpsertBody>,
) -> Result<Json<IdResponse>, (StatusCode, String)> {
    let id = rust_http_bench::upsert(&pool, body.id, &body.name, &body.email)
        .await
        .map_err(internal)?;
    Ok(Json(IdResponse { id: Some(id) }))
}

async fn delete_row(
    State(pool): State<PgPool>,
    Path(id): Path<i32>,
) -> Result<Json<OkResponse>, (StatusCode, String)> {
    rust_http_bench::delete(&pool, id).await.map_err(internal)?;
    Ok(Json(OkResponse { ok: true }))
}

fn internal(err: sqlx::Error) -> (StatusCode, String) {
    (StatusCode::INTERNAL_SERVER_ERROR, err.to_string())
}

async fn update_any(
    State(pool): State<PgPool>,
    Json(body): Json<NewRowBody>,
) -> Result<Json<IdResponse>, (StatusCode, String)> {
    let id = rust_http_bench::next_spread_id();
    let out = rust_http_bench::update(&pool, id, &body.name, &body.email)
        .await
        .map_err(internal)?;
    Ok(Json(IdResponse { id: out }))
}

async fn upsert_any(
    State(pool): State<PgPool>,
    Json(body): Json<UpsertBody>,
) -> Result<Json<IdResponse>, (StatusCode, String)> {
    let id = rust_http_bench::next_spread_id();
    let out = rust_http_bench::upsert(&pool, id, &body.name, &body.email)
        .await
        .map_err(internal)?;
    Ok(Json(IdResponse { id: Some(out) }))
}

async fn delete_oldest(State(pool): State<PgPool>) -> Result<Json<OkResponse>, (StatusCode, String)> {
    rust_http_bench::delete_oldest(&pool).await.map_err(internal)?;
    Ok(Json(OkResponse { ok: true }))
}

#[tokio::main]
async fn main() {
    let port = rust_http_bench::port_from_args(8261);
    let pool_size = rust_http_bench::pool_from_args(16);
    let pool = rust_http_bench::connect(pool_size).await;

    let app = Router::new()
        .route("/ping", get(ping))
        .route("/me", get(me))
        .route("/rows", get(list_rows).post(insert_row))
        .route("/rows/upsert", post(upsert_row))
        .route("/rows/oldest", axum::routing::delete(delete_oldest))
        .route("/rows/any", put(update_any))
        .route("/rows/upsert-any", post(upsert_any))
        .route("/rows/{id}", put(update_row).delete(delete_row))
        .with_state(pool);

    let listener = tokio::net::TcpListener::bind(("127.0.0.1", port))
        .await
        .expect("bind axum");
    println!("axum_srv: порт {port}, пул {pool_size}");
    axum::serve(listener, app).await.expect("axum serve");
}
