//! Вложенный модуль `ferronit._core.db`: запросы в PostgreSQL и разбор результата в Rust,
//! JSON на выходе. Публичное имя — `ferronit.db`.
//!
//! Зачем: разбор строк результата в Python стоит ~15,7 мкс на строку, в компилируемом
//! коде — ~1,7 мкс (см. `bench/README.md`). Здесь запрос выполняется в Rust
//! (tokio-postgres через пул `deadpool-postgres`), а Python получает готовую JSON-строку:
//! ни маппинга, ни сериализации под GIL.
//!
//! Пул: `deadpool-postgres` держит набор соединений и отдаёт их с recycle-проверкой;
//! `Client::prepare_cached` кэширует prepared statements на соединении (как asyncpg,
//! у которого кэш включён по умолчанию) — это +20–30 % на чтении против `client.query`.
//!
//! Типы: int/float/bool/text, `NUMERIC` (без потери точности), `JSON/JSONB`, `UUID`,
//! дата/время, `bytea` (hex) и одномерные массивы; `NULL` → `null`. TLS — rustls с
//! системными корневыми сертификатами; `?sslmode=disable` отключает шифрование.
//!
//! Использование из asyncio-приложения:
//!     import ferronit
//!     ferronit.db.connect("postgresql://user@host/db", 16)   # один раз на старте
//!     body = await asyncio.to_thread(ferronit.db.query_json, "SELECT id, name FROM t", [])
//!
//! Модель параллелизма: `query_json` — синхронная функция, которая снимает GIL
//! (`py.allow_threads`) и блокирующе ждёт результат из фонового tokio-рантайма.
//! Поэтому из асинхронного кода её надо вызывать через `asyncio.to_thread`, иначе
//! она заблокирует цикл событий.

use std::error::Error;
use std::sync::OnceLock;

use deadpool_postgres::{Config, ManagerConfig, Pool, RecyclingMethod, Runtime as PoolRuntime};
use pyo3::exceptions::{PyRuntimeError, PyTypeError};
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyBool};
use serde_json::{json, Map, Value};
use tokio::runtime::Runtime;
use tokio_postgres::types::{FromSql, ToSql, Type};
use tokio_postgres::NoTls;
use tokio_postgres_rustls::MakeRustlsConnect;

static RUNTIME: OnceLock<Runtime> = OnceLock::new();
static POOL: OnceLock<Pool> = OnceLock::new();

/// Фоновый tokio-рантайм: на нём живут соединения пула и выполняется ожидание запросов.
fn runtime() -> &'static Runtime {
    RUNTIME.get_or_init(|| {
        tokio::runtime::Builder::new_multi_thread()
            .worker_threads(4)
            .enable_all()
            .build()
            .expect("не удалось поднять tokio-рантайм")
    })
}

type SqlParam = Box<dyn ToSql + Sync + Send>;

/// Создать пул соединений `deadpool-postgres` с быстрой recycle-проверкой.
fn build_pool(dsn: &str, size: usize, use_tls: bool) -> Result<Pool, String> {
    let mut config = Config::new();
    config.url = Some(dsn.to_string());
    config.pool = Some(deadpool_postgres::PoolConfig::new(size));
    config.manager = Some(ManagerConfig {
        recycling_method: RecyclingMethod::Fast,
    });
    let result = if use_tls {
        config.create_pool(Some(PoolRuntime::Tokio1), rustls_connector()?)
    } else {
        config.create_pool(Some(PoolRuntime::Tokio1), NoTls)
    };
    result.map_err(|error| error.to_string())
}

/// rustls-коннектор с системными корневыми сертификатами.
fn rustls_connector() -> Result<MakeRustlsConnect, String> {
    let mut roots = rustls::RootCertStore::empty();
    let loaded = rustls_native_certs::load_native_certs();
    for certificate in loaded.certs {
        roots.add(certificate).map_err(|error| error.to_string())?;
    }
    if roots.is_empty() {
        return Err("системные корневые сертификаты не найдены".to_string());
    }
    let config = rustls::ClientConfig::builder()
        .with_root_certificates(roots)
        .with_no_client_auth();
    Ok(MakeRustlsConnect::new(config))
}

/// Инициализировать пул соединений (вызывается один раз на старте приложения).
#[pyfunction]
fn connect(dsn: String, pool_size: usize) -> PyResult<()> {
    // Внутри рантайма: deadpool цепляет Handle текущего tokio-рантайма для spawn соединений.
    // TLS: `sslmode=disable` — без шифрования, иначе rustls (по умолчанию Postgres — prefer).
    let use_tls = !dsn.to_ascii_lowercase().contains("sslmode=disable");
    let pool = runtime()
        .block_on(async { build_pool(&dsn, pool_size, use_tls) })
        .map_err(PyRuntimeError::new_err)?;
    if POOL.set(pool).is_err() {
        return Err(PyRuntimeError::new_err("ferronit.db.connect уже вызван"));
    }
    Ok(())
}

/// Выполнить запрос и вернуть результат JSON-строкой (массив объектов).
///
/// Statement кэшируется на соединении (`prepare_cached`), поэтому повторные вызовы
/// с тем же SQL не платят за parse/plan.
#[pyfunction]
fn query_json(py: Python<'_>, sql: String, params: Vec<Py<PyAny>>) -> PyResult<String> {
    let sql_params = params_to_sql(py, &params)?;
    let pool = POOL
        .get()
        .cloned()
        .ok_or_else(|| PyRuntimeError::new_err("сначала вызовите ferronit.db.connect(...)"))?;
    py.allow_threads(|| {
        runtime().block_on(async move {
            let client = pool.get().await.map_err(|error| error.to_string())?;
            let statement = client
                .prepare_cached(&sql)
                .await
                .map_err(|error| error.to_string())?;
            let refs: Vec<&(dyn ToSql + Sync)> = sql_params
                .iter()
                .map(|param| param.as_ref() as &(dyn ToSql + Sync))
                .collect();
            let rows = client
                .query(&statement, &refs)
                .await
                .map_err(|error| error.to_string())?;
            let values: Vec<Value> = rows.iter().map(row_to_value).collect();
            serde_json::to_string(&values).map_err(|error| error.to_string())
        })
    })
    .map_err(PyRuntimeError::new_err)
}

/// Двоичные коды знака `NUMERIC` (см. формат PostgreSQL).
const NUMERIC_NAN: u16 = 0xC000;
const NUMERIC_PINF: u16 = 0xD000;
const NUMERIC_NINF: u16 = 0xF000;
const NUMERIC_NEG: u16 = 0x4000;

/// `NUMERIC` в двоичном формате PostgreSQL → строка без потери точности.
struct PgNumeric(String);

impl<'a> FromSql<'a> for PgNumeric {
    fn from_sql(_ty: &Type, raw: &'a [u8]) -> Result<Self, Box<dyn Error + Sync + Send>> {
        decode_numeric(raw).map(Self).map_err(Into::into)
    }

    fn accepts(ty: &Type) -> bool {
        *ty == Type::NUMERIC
    }
}

/// Разобрать двоичный `NUMERIC`: заголовок (ndigits, weight, sign, dscale) плюс цифры
/// по основанию 10000. Возвращает десятичную строку либо спецзначение.
fn decode_numeric(raw: &[u8]) -> Result<String, String> {
    if raw.len() < 8 {
        return Err("numeric: короткий заголовок".to_string());
    }
    let ndigits = i16::from_be_bytes([raw[0], raw[1]]) as i32;
    let weight = i16::from_be_bytes([raw[2], raw[3]]) as i32;
    let sign = u16::from_be_bytes([raw[4], raw[5]]);
    let dscale = u16::from_be_bytes([raw[6], raw[7]]) as i32;
    match sign {
        NUMERIC_NAN => return Ok("NaN".to_string()),
        NUMERIC_PINF => return Ok("Infinity".to_string()),
        NUMERIC_NINF => return Ok("-Infinity".to_string()),
        _ => {}
    }
    let int_groups = (weight + 1).max(0);
    if int_groups > 100_000 {
        return Err("numeric: неправдоподобный вес".to_string());
    }
    let digits: Vec<i32> = (0..ndigits)
        .map(|index| {
            let offset = 8 + (index as usize) * 2;
            match raw.get(offset..offset + 2) {
                Some(pair) => i16::from_be_bytes([pair[0], pair[1]]) as i32,
                None => 0,
            }
        })
        .collect();
    // digit[i] соответствует разряду (weight - i) по основанию 10000
    let group = |position: i32| -> i32 {
        let index = weight - position;
        if index >= 0 && (index as usize) < digits.len() {
            digits[index as usize]
        } else {
            0
        }
    };

    let mut integer = String::new();
    for position in (0..int_groups).rev() {
        let digit = group(position);
        if position == int_groups - 1 {
            integer.push_str(&digit.to_string());
        } else {
            integer.push_str(&format!("{digit:04}"));
        }
    }
    if integer.is_empty() {
        integer.push('0');
    }

    let mut out = String::new();
    if sign == NUMERIC_NEG {
        out.push('-');
    }
    out.push_str(&integer);
    if dscale > 0 {
        let mut fraction = String::new();
        for step in 1..=((dscale + 3) / 4) {
            fraction.push_str(&format!("{:04}", group(-step)));
        }
        fraction.truncate(dscale as usize);
        out.push('.');
        out.push_str(&fraction);
    }
    Ok(out)
}

/// Байты в hex (для `bytea`).
fn to_hex(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push(HEX[(byte >> 4) as usize] as char);
        out.push(HEX[(byte & 0x0f) as usize] as char);
    }
    out
}

/// Преобразовать строку результата в JSON-объект.
///
/// Значения читаются как `Option<T>`, поэтому `NULL` становится `null`, а не роняет
/// запрос. `NUMERIC` разбирается в строку без потери точности, дата/время и `UUID` —
/// в ISO-строку, `JSON/JSONB` — как есть, `bytea` — в hex. Типы, которых нет в списке,
/// читаются как текст, а нечитаемое значение становится `null`.
fn row_to_value(row: &tokio_postgres::Row) -> Value {
    let mut map = Map::new();
    for (index, column) in row.columns().iter().enumerate() {
        let ty = column.type_();
        let value = if *ty == Type::INT2 {
            json!(row.get::<_, Option<i16>>(index))
        } else if *ty == Type::INT4 {
            json!(row.get::<_, Option<i32>>(index))
        } else if *ty == Type::INT8 {
            json!(row.get::<_, Option<i64>>(index))
        } else if *ty == Type::BOOL {
            json!(row.get::<_, Option<bool>>(index))
        } else if *ty == Type::FLOAT4 {
            json!(row.get::<_, Option<f32>>(index))
        } else if *ty == Type::FLOAT8 {
            json!(row.get::<_, Option<f64>>(index))
        } else if *ty == Type::NUMERIC {
            json!(row
                .try_get::<_, Option<PgNumeric>>(index)
                .ok()
                .flatten()
                .map(|number| number.0))
        } else if *ty == Type::JSON || *ty == Type::JSONB {
            json!(row.get::<_, Option<serde_json::Value>>(index))
        } else if *ty == Type::UUID {
            json!(row
                .get::<_, Option<uuid::Uuid>>(index)
                .map(|value| value.to_string()))
        } else if *ty == Type::DATE {
            json!(row
                .get::<_, Option<chrono::NaiveDate>>(index)
                .map(|value| value.to_string()))
        } else if *ty == Type::TIME {
            json!(row
                .get::<_, Option<chrono::NaiveTime>>(index)
                .map(|value| value.to_string()))
        } else if *ty == Type::TIMESTAMP {
            json!(row
                .get::<_, Option<chrono::NaiveDateTime>>(index)
                .map(|value| value.to_string()))
        } else if *ty == Type::TIMESTAMPTZ {
            json!(row
                .get::<_, Option<chrono::DateTime<chrono::Utc>>>(index)
                .map(|value| value.to_rfc3339()))
        } else if *ty == Type::BYTEA {
            json!(row
                .get::<_, Option<Vec<u8>>>(index)
                .map(|value| to_hex(&value)))
        } else if *ty == Type::INT2_ARRAY {
            json!(row.get::<_, Option<Vec<Option<i16>>>>(index))
        } else if *ty == Type::INT4_ARRAY {
            json!(row.get::<_, Option<Vec<Option<i32>>>>(index))
        } else if *ty == Type::INT8_ARRAY {
            json!(row.get::<_, Option<Vec<Option<i64>>>>(index))
        } else if *ty == Type::BOOL_ARRAY {
            json!(row.get::<_, Option<Vec<Option<bool>>>>(index))
        } else if *ty == Type::FLOAT4_ARRAY {
            json!(row.get::<_, Option<Vec<Option<f32>>>>(index))
        } else if *ty == Type::FLOAT8_ARRAY {
            json!(row.get::<_, Option<Vec<Option<f64>>>>(index))
        } else if *ty == Type::TEXT_ARRAY || *ty == Type::VARCHAR_ARRAY {
            json!(row.get::<_, Option<Vec<Option<String>>>>(index))
        } else if *ty == Type::JSON_ARRAY || *ty == Type::JSONB_ARRAY {
            json!(row.get::<_, Option<Vec<Option<serde_json::Value>>>>(index))
        } else if *ty == Type::UUID_ARRAY {
            json!(row
                .get::<_, Option<Vec<Option<uuid::Uuid>>>>(index)
                .map(|values| values
                    .into_iter()
                    .map(|value| value.map(|value| value.to_string()))
                    .collect::<Vec<_>>()))
        } else if *ty == Type::NUMERIC_ARRAY {
            json!(row
                .get::<_, Option<Vec<Option<PgNumeric>>>>(index)
                .map(|values| values
                    .into_iter()
                    .map(|value| value.map(|number| number.0))
                    .collect::<Vec<_>>()))
        } else {
            match row.try_get::<_, Option<String>>(index) {
                Ok(text) => json!(text),
                Err(_) => Value::Null,
            }
        };
        map.insert(column.name().to_string(), value);
    }
    Value::Object(map)
}

fn params_to_sql(py: Python<'_>, params: &[Py<PyAny>]) -> PyResult<Vec<SqlParam>> {
    let mut out: Vec<SqlParam> = Vec::with_capacity(params.len());
    for param in params {
        let object = param.bind(py);
        if object.is_none() {
            out.push(Box::new(None::<String>));
        } else if object.is_instance_of::<PyBool>() {
            // bool раньше int: в Python True/False — подкласс int
            out.push(Box::new(object.extract::<bool>()?));
        } else if let Ok(value) = object.extract::<String>() {
            out.push(Box::new(value));
        } else if let Ok(value) = object.extract::<i32>() {
            out.push(Box::new(value));
        } else if let Ok(value) = object.extract::<i64>() {
            out.push(Box::new(value));
        } else if let Ok(value) = object.extract::<f64>() {
            out.push(Box::new(value));
        } else {
            return Err(PyTypeError::new_err(
                "неподдерживаемый тип параметра (есть str/int/float/bool/None)",
            ));
        }
    }
    Ok(out)
}

/// Зарегистрировать функции `connect` и `query_json` на вложенном модуле `ferronit._core.db`.
pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(connect, module)?)?;
    module.add_function(wrap_pyfunction!(query_json, module)?)?;
    Ok(())
}
