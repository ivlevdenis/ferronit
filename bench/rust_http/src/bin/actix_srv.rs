//! HTTP-стенд на **Actix Web** — те же маршруты и та же таблица, что у Python/Go-стендов.
//!
//! Запуск: `target/release/actix_srv --port 8262 --pool 16`
//! Один воркер — `.workers(1)`, чтобы сравнивать с одним процессом Ferronit/granian.

use actix_web::{web, App, HttpResponse, HttpServer, Responder};
use serde::{Deserialize, Serialize};
use sqlx::PgPool;

use rust_http_bench::{DEFAULT_LIMIT, RowOut};

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

type ApiResult = Result<HttpResponse, actix_web::Error>;

fn fail(err: sqlx::Error) -> actix_web::Error {
    actix_web::error::ErrorInternalServerError(err.to_string())
}

async fn ping() -> impl Responder {
    HttpResponse::Ok().json(OkResponse { ok: true })
}

async fn list_rows(pool: web::Data<PgPool>, q: web::Query<LimitQuery>) -> ApiResult {
    let limit = q.limit.unwrap_or(DEFAULT_LIMIT);
    let rows = rust_http_bench::list(&pool, limit).await.map_err(fail)?;
    Ok(HttpResponse::Ok().json(RowsResponse { rows }))
}

async fn insert_row(pool: web::Data<PgPool>, body: web::Json<NewRowBody>) -> ApiResult {
    let id = rust_http_bench::insert(&pool, &body.name, &body.email)
        .await
        .map_err(fail)?;
    Ok(HttpResponse::Ok().json(IdResponse { id: Some(id) }))
}

async fn update_row(
    pool: web::Data<PgPool>,
    id: web::Path<i32>,
    body: web::Json<NewRowBody>,
) -> ApiResult {
    let out = rust_http_bench::update(&pool, id.into_inner(), &body.name, &body.email)
        .await
        .map_err(fail)?;
    Ok(HttpResponse::Ok().json(IdResponse { id: out }))
}

async fn upsert_row(pool: web::Data<PgPool>, body: web::Json<UpsertBody>) -> ApiResult {
    let id = rust_http_bench::upsert(&pool, body.id, &body.name, &body.email)
        .await
        .map_err(fail)?;
    Ok(HttpResponse::Ok().json(IdResponse { id: Some(id) }))
}

async fn delete_row(pool: web::Data<PgPool>, id: web::Path<i32>) -> ApiResult {
    rust_http_bench::delete(&pool, id.into_inner())
        .await
        .map_err(fail)?;
    Ok(HttpResponse::Ok().json(OkResponse { ok: true }))
}

async fn update_any(pool: web::Data<PgPool>, body: web::Json<NewRowBody>) -> ApiResult {
    let id = rust_http_bench::next_spread_id();
    let out = rust_http_bench::update(&pool, id, &body.name, &body.email)
        .await
        .map_err(fail)?;
    Ok(HttpResponse::Ok().json(IdResponse { id: out }))
}

async fn upsert_any(pool: web::Data<PgPool>, body: web::Json<UpsertBody>) -> ApiResult {
    let id = rust_http_bench::next_spread_id();
    let out = rust_http_bench::upsert(&pool, id, &body.name, &body.email)
        .await
        .map_err(fail)?;
    Ok(HttpResponse::Ok().json(IdResponse { id: Some(out) }))
}

async fn delete_oldest(pool: web::Data<PgPool>) -> ApiResult {
    rust_http_bench::delete_oldest(&pool).await.map_err(fail)?;
    Ok(HttpResponse::Ok().json(OkResponse { ok: true }))
}

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    let port = rust_http_bench::port_from_args(8262);
    let pool_size = rust_http_bench::pool_from_args(16);
    let workers = rust_http_bench::workers_from_args(1);
    let pool = rust_http_bench::connect(pool_size).await;
    let shared = web::Data::new(pool);

    println!("actix_srv: порт {port}, пул {pool_size}, воркеров {workers}");
    HttpServer::new(move || {
        App::new()
            .app_data(shared.clone())
            .route("/ping", web::get().to(ping))
            .route("/rows", web::get().to(list_rows))
            .route("/rows", web::post().to(insert_row))
            .route("/rows/upsert", web::post().to(upsert_row))
            .route("/rows/any", web::put().to(update_any))
            .route("/rows/upsert-any", web::post().to(upsert_any))
            .route("/rows/oldest", web::delete().to(delete_oldest))
            .route("/rows/{id}", web::put().to(update_row))
            .route("/rows/{id}", web::delete().to(delete_row))
    })
    .workers(workers)
    .bind(("127.0.0.1", port))?
    .run()
    .await
}
