// tokio-postgres: одна строка по id (10 колонок, users_bench), 1 поток.
use std::time::{Instant, SystemTime};

use tokio_postgres::NoTls;

const DSN: &str = "postgres://postgres:postgres@127.0.0.1:5432/postgres";
const SELECT_SQL: &str = "SELECT id, name, email, active, score, age, city, created_at, bio, flags \
     FROM users_bench WHERE id = $1";

fn scan_row(row: &tokio_postgres::Row) {
    let _id: i32 = row.get(0);
    let _name: String = row.get(1);
    let _email: String = row.get(2);
    let _active: bool = row.get(3);
    let _score: i32 = row.get(4);
    let _age: i32 = row.get(5);
    let _city: String = row.get(6);
    let _created_at: SystemTime = row.get(7);
    let _bio: String = row.get(8);
    let _flags: serde_json::Value = row.get(9);
}

#[tokio::main(flavor = "multi_thread", worker_threads = 1)]
async fn main() {
    let (client, connection) = tokio_postgres::connect(DSN, NoTls).await.expect("connect");
    tokio::spawn(connection);

    // prepared statement (extended protocol), как это делают asyncpg/pgx в кэше
    let stmt = client.prepare(SELECT_SQL).await.expect("prepare");

    const N: usize = 20000;

    for _ in 0..1000 {
        let _ = client.query_one(&stmt, &[&1i32]).await.unwrap();
    }

    let mut rates = Vec::new();
    for _ in 0..5 {
        let t0 = Instant::now();
        for _ in 0..N {
            let row = client.query_one(&stmt, &[&1i32]).await.unwrap();
            scan_row(&row);
        }
        rates.push(N as f64 / t0.elapsed().as_secs_f64());
    }
    rates.sort_by(|a, b| a.partial_cmp(b).unwrap());
    println!(
        "Rust tokio-postgres, 1 поток, 1 строка (id=1):  {:6.0} req/s (медиана из 5)",
        rates[2]
    );
    println!("  прогоны: {:?}", rates);
}
