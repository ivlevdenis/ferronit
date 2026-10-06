//! Прямой замер слоёв доступа к PostgreSQL на Rust: сырой драйвер, sqlx, SeaORM.
//!
//! Мерим ровно то же, что и Python-стенд (`bench/pg_direct.py`): один запрос
//! `SELECT id, name, email FROM users LIMIT 100` (или INSERT), N клиентов, M запросов
//! на клиента, тот же инстанс PostgreSQL — чтобы числа ложились в одну таблицу с Python.
//!
//! Ключевая деталь честности: рантайм tokio можно ограничить одним рабочим потоком
//! (`--threads 1`). Тогда клиентская сторона работает так же, как asyncio в одном
//! Python-процессе, и сравнение слоёв становится корректным.
//!
//! Запуск:
//!   cargo run --release -- --mode sqlx --op select --clients 10 --requests 1000 --threads 1

use std::time::Instant;

const DSN: &str = "postgres://postgres:postgres@127.0.0.1:5432/postgres";
const SELECT_SQL: &str = "SELECT id, name, email FROM users LIMIT 100";
const INSERT_SQL: &str = "INSERT INTO users (name, email) VALUES ($1, $2) RETURNING id, name";

use rust_pg_bench::entity::{ActiveModel, Entity};

struct Args {
    mode: String,
    op: String,
    clients: usize,
    requests: usize,
    threads: usize,
}

fn parse_args() -> Args {
    let mut args = Args {
        mode: "sqlx".to_string(),
        op: "select".to_string(),
        clients: 10,
        requests: 1000,
        threads: 1,
    };
    let argv: Vec<String> = std::env::args().collect();
    let mut i = 1;
    while i + 1 < argv.len() {
        let value = argv[i + 1].clone();
        match argv[i].as_str() {
            "--mode" => args.mode = value,
            "--op" => args.op = value,
            "--clients" => args.clients = value.parse().expect("--clients должен быть числом"),
            "--requests" => args.requests = value.parse().expect("--requests должен быть числом"),
            "--threads" => args.threads = value.parse().expect("--threads должен быть числом"),
            other => panic!("неизвестный аргумент: {other}"),
        }
        i += 2;
    }
    args
}

fn report(mode: &str, op: &str, clients: usize, requests: usize, threads: usize, wall: f64, mut durations: Vec<f64>) {
    durations.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let total = durations.len() as f64;
    let quantile = |p: f64| -> f64 {
        let idx = (((total * p) as usize).min(durations.len() - 1)) as usize;
        durations[idx] / 1000.0 // мкс → мс
    };
    println!(
        "Rust {mode:<15} {op:<6} клиентов={clients:<3} потоков={threads:<2} запросов={requests:<5} -> {:>9.0} зап/с   p50 {:.2} мс   p95 {:.2} мс",
        total / wall,
        quantile(0.50),
        quantile(0.95),
    );
}

async fn run_tokio_pg(op: &str, clients: usize, requests: usize, cached: bool) -> Vec<f64> {
    use tokio_postgres::NoTls;

    let mut handles = Vec::with_capacity(clients);
    for client_id in 0..clients {
        let op = op.to_string();
        handles.push(tokio::spawn(async move {
            let (client, connection) = tokio_postgres::connect(DSN, NoTls).await.expect("подключение");
            tokio::spawn(connection); // соединение живёт в фоне
            // Кэш prepared statements: `client.query(sql)` готовит statement на каждый
            // вызов, а `client.prepare(sql)` — один раз. Держим Statement живым весь цикл.
            let (select_stmt, insert_stmt) = if cached {
                if op == "select" {
                    (Some(client.prepare(SELECT_SQL).await.expect("prepare")), None)
                } else {
                    (None, Some(client.prepare(INSERT_SQL).await.expect("prepare")))
                }
            } else {
                (None, None)
            };
            let mut durations = Vec::with_capacity(requests);
            for req in 0..requests {
                let t0 = Instant::now();
                if op == "select" {
                    let rows = match &select_stmt {
                        Some(stmt) => client.query(stmt, &[]).await.expect("select"),
                        None => client.query(SELECT_SQL, &[]).await.expect("select"),
                    };
                    let _mapped: Vec<(i32, String, String)> = rows
                        .iter()
                        .map(|row| (row.get(0), row.get(1), row.get(2)))
                        .collect();
                } else {
                    let name = format!("user_{client_id}_{req}");
                    let email = format!("u{client_id}_{req}@example.com");
                    match &insert_stmt {
                        Some(stmt) => {
                            client.query_one(stmt, &[&name, &email]).await.expect("insert");
                        }
                        None => {
                            client
                                .query_one(INSERT_SQL, &[&name, &email])
                                .await
                                .expect("insert");
                        }
                    }
                }
                durations.push(t0.elapsed().as_micros() as f64);
            }
            durations
        }));
    }
    let mut all = Vec::with_capacity(clients * requests);
    for handle in handles {
        all.extend(handle.await.expect("задача"));
    }
    all
}

async fn run_sqlx(op: &str, clients: usize, requests: usize) -> Vec<f64> {
    use sqlx::postgres::PgPoolOptions;
    use sqlx::Row;

    let pool = PgPoolOptions::new()
        .max_connections(clients as u32)
        .connect(DSN)
        .await
        .expect("пул sqlx");

    let mut handles = Vec::with_capacity(clients);
    for client_id in 0..clients {
        let pool = pool.clone();
        let op = op.to_string();
        handles.push(tokio::spawn(async move {
            let mut durations = Vec::with_capacity(requests);
            for req in 0..requests {
                let t0 = Instant::now();
                if op == "select" {
                    let rows = sqlx::query(SELECT_SQL).fetch_all(&pool).await.expect("select");
                    let _mapped: Vec<(i32, String, String)> = rows
                        .iter()
                        .map(|row| (row.get::<i32, _>(0), row.get::<String, _>(1), row.get::<String, _>(2)))
                        .collect();
                } else {
                    let name = format!("user_{client_id}_{req}");
                    let email = format!("u{client_id}_{req}@example.com");
                    sqlx::query(INSERT_SQL)
                        .bind(&name)
                        .bind(&email)
                        .fetch_one(&pool)
                        .await
                        .expect("insert");
                }
                durations.push(t0.elapsed().as_micros() as f64);
            }
            durations
        }));
    }
    let mut all = Vec::with_capacity(clients * requests);
    for handle in handles {
        all.extend(handle.await.expect("задача"));
    }
    all
}

async fn run_seaorm(op: &str, clients: usize, requests: usize) -> Vec<f64> {
    use sea_orm::{ActiveModelTrait, Database, EntityTrait, QuerySelect, Set};

    let db = Database::connect(DSN).await.expect("подключение SeaORM");

    let mut handles = Vec::with_capacity(clients);
    for client_id in 0..clients {
        let db = db.clone();
        let op = op.to_string();
        handles.push(tokio::spawn(async move {
            let mut durations = Vec::with_capacity(requests);
            for req in 0..requests {
                let t0 = Instant::now();
                if op == "select" {
                    let _rows = Entity::find().limit(100).all(&db).await.expect("select");
                } else {
                    let model = ActiveModel {
                        name: Set(format!("user_{client_id}_{req}")),
                        email: Set(format!("u{client_id}_{req}@example.com")),
                        ..Default::default()
                    };
                    model.insert(&db).await.expect("insert");
                }
                durations.push(t0.elapsed().as_micros() as f64);
            }
            durations
        }));
    }
    let mut all = Vec::with_capacity(clients * requests);
    for handle in handles {
        all.extend(handle.await.expect("задача"));
    }
    all
}

fn main() {
    let args = parse_args();
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(args.threads)
        .enable_all()
        .build()
        .expect("рантайм tokio");

    let started = Instant::now();
    let durations = runtime.block_on(async {
        match args.mode.as_str() {
            "tokio-postgres" => run_tokio_pg(&args.op, args.clients, args.requests, false).await,
            "tokio-postgres-cached" => run_tokio_pg(&args.op, args.clients, args.requests, true).await,
            "sqlx" => run_sqlx(&args.op, args.clients, args.requests).await,
            "seaorm" => run_seaorm(&args.op, args.clients, args.requests).await,
            other => panic!(
                "неизвестный режим: {other} (есть: tokio-postgres, tokio-postgres-cached, sqlx, seaorm)"
            ),
        }
    });
    report(
        &args.mode,
        &args.op,
        args.clients,
        args.requests,
        args.threads,
        started.elapsed().as_secs_f64(),
        durations,
    );
}
