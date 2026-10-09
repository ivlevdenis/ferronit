//! HTTP-сервис на axum с теми же маршрутами, что у Ferronit в `bench_postgres.py`:
//! `GET /users` (SELECT 100 строк), `POST /users` (INSERT + RETURNING), `GET /ping` (без базы).
//!
//! Смысл: сравнить приложения целиком, а не только слои доступа. Python-сторона — granian
//! в один воркер (`bench_postgres.py`), здесь можно задать число потоков tokio явно.
//!
//! Запуск:
//!   cargo run --release --bin http_service -- --mode raw --threads 1 --port 8201 --pool 16

use std::sync::Arc;

use axum::{extract::State, routing::get, Json, Router};
use sea_orm::{ActiveModelTrait, Database, DatabaseConnection, EntityTrait, QuerySelect, Set};
use serde::{Deserialize, Serialize};
use tokio::sync::{mpsc, Mutex};
use tokio_postgres::NoTls;

use rust_pg_bench::entity::{ActiveModel, Entity};

const DSN: &str = "postgres://postgres:postgres@127.0.0.1:5432/postgres";
const SELECT_SQL: &str = "SELECT id, name, email FROM users LIMIT 100";
const INSERT_SQL: &str = "INSERT INTO users (name, email) VALUES ($1, $2) RETURNING id, name";

#[derive(Serialize)]
struct User {
    id: i32,
    name: String,
    email: String,
}

#[derive(Serialize)]
struct UsersResponse {
    users: Vec<User>,
}

#[derive(Serialize)]
struct Created {
    id: i32,
    name: String,
}

#[derive(Serialize)]
struct Pong {
    ok: bool,
}

#[derive(Deserialize)]
struct NewUser {
    name: String,
    email: String,
}

// ── режим raw: сырой драйвер, пул из канала ──────────────────────────────────

#[derive(Clone)]
struct RawState {
    tx: mpsc::Sender<tokio_postgres::Client>,
    rx: Arc<Mutex<mpsc::Receiver<tokio_postgres::Client>>>,
}

impl RawState {
    async fn new(size: usize) -> Self {
        let (tx, rx) = mpsc::channel(size);
        for _ in 0..size {
            let (client, connection) = tokio_postgres::connect(DSN, NoTls).await.expect("подключение");
            tokio::spawn(connection);
            tx.send(client).await.expect("наполнение пула");
        }
        Self {
            tx,
            rx: Arc::new(Mutex::new(rx)),
        }
    }

    async fn take(&self) -> tokio_postgres::Client {
        self.rx.lock().await.recv().await.expect("пул пуст")
    }

    async fn put(&self, client: tokio_postgres::Client) {
        let _ = self.tx.send(client).await;
    }
}

async fn raw_users(State(state): State<RawState>) -> Json<UsersResponse> {
    let client = state.take().await;
    let rows = client.query(SELECT_SQL, &[]).await.expect("select");
    let users = rows
        .iter()
        .map(|row| User {
            id: row.get(0),
            name: row.get(1),
            email: row.get(2),
        })
        .collect();
    state.put(client).await;
    Json(UsersResponse { users })
}

async fn raw_create(State(state): State<RawState>, Json(body): Json<NewUser>) -> Json<Created> {
    let client = state.take().await;
    let row = client
        .query_one(INSERT_SQL, &[&body.name, &body.email])
        .await
        .expect("insert");
    let created = Created {
        id: row.get(0),
        name: row.get(1),
    };
    state.put(client).await;
    Json(created)
}

// ── режим seaorm: тот же маршрут через ORM ───────────────────────────────────

#[derive(Clone)]
struct OrmState {
    db: DatabaseConnection,
}

async fn orm_users(State(state): State<OrmState>) -> Json<UsersResponse> {
    let rows = Entity::find()
        .limit(100)
        .all(&state.db)
        .await
        .expect("select");
    let users = rows
        .into_iter()
        .map(|model| User {
            id: model.id,
            name: model.name,
            email: model.email,
        })
        .collect();
    Json(UsersResponse { users })
}

async fn orm_create(State(state): State<OrmState>, Json(body): Json<NewUser>) -> Json<Created> {
    let model = ActiveModel {
        name: Set(body.name),
        email: Set(body.email),
        ..Default::default()
    };
    let inserted = model.insert(&state.db).await.expect("insert");
    Json(Created {
        id: inserted.id,
        name: inserted.name,
    })
}

// ── запуск ───────────────────────────────────────────────────────────────────

struct Args {
    mode: String,
    threads: usize,
    port: u16,
    pool: usize,
}

fn parse_args() -> Args {
    let mut args = Args {
        mode: "raw".to_string(),
        threads: 1,
        port: 8201,
        pool: 16,
    };
    let argv: Vec<String> = std::env::args().collect();
    let mut i = 1;
    while i + 1 < argv.len() {
        let value = argv[i + 1].clone();
        match argv[i].as_str() {
            "--mode" => args.mode = value,
            "--threads" => args.threads = value.parse().expect("--threads числом"),
            "--port" => args.port = value.parse().expect("--port числом"),
            "--pool" => args.pool = value.parse().expect("--pool числом"),
            other => panic!("неизвестный аргумент: {other}"),
        }
        i += 2;
    }
    args
}

fn main() {
    let args = parse_args();
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(args.threads)
        .enable_all()
        .build()
        .expect("рантайм tokio");

    let mode = args.mode.clone();
    let threads = args.threads;
    let port = args.port;
    let pool = args.pool;

    runtime.block_on(async move {
        let app: Router = match mode.as_str() {
            "raw" => {
                let state = RawState::new(pool).await;
                Router::new()
                    .route("/users", get(raw_users).post(raw_create))
                    .route("/ping", get(|| async { Json(Pong { ok: true }) }))
                    .with_state(state)
            }
            "seaorm" => {
                let state = OrmState {
                    db: Database::connect(DSN).await.expect("подключение SeaORM"),
                };
                Router::new()
                    .route("/users", get(orm_users).post(orm_create))
                    .route("/ping", get(|| async { Json(Pong { ok: true }) }))
                    .with_state(state)
            }
            other => panic!("неизвестный режим: {other} (есть: raw, seaorm)"),
        };

        let listener = tokio::net::TcpListener::bind(format!("127.0.0.1:{port}"))
            .await
            .expect("порт занят");
        println!("http_service готов: режим {mode}, потоков {threads}, порт {port}, пул {pool}");
        axum::serve(listener, app).await.expect("сервер");
    });
}
