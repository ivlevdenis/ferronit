// Package benchapi — общий для всех Go-HTTP-вариантов слой: pgx-пул, запросы
// к bench_rows, типы ответов и проверка JWT. HTTP-адаптеры (chi/echo/fiber/
// fasthttp/gin) живут в cmd/* и отличаются только роутингом и биндингом.
package benchapi

import (
	"context"
	"database/sql/driver"
	"fmt"
	"os"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"github.com/golang-jwt/jwt/v5"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/jackc/pgx/v5/pgxpool"
)

const (
	// DSN — тот же PostgreSQL, что у Python-стенда.
	DSN = "postgres://postgres:postgres@127.0.0.1:5432/postgres"

	columns   = "id, name, email, created_at, birthday, ttl"
	selectSQL = "SELECT " + columns + " FROM bench_rows LIMIT 100"
	rowsSQL   = "SELECT " + columns + " FROM bench_rows LIMIT $1"
	insertSQL = "INSERT INTO bench_rows (name, email, created_at, birthday, ttl) " +
		"VALUES ($1, $2, $3, $4, $5) RETURNING id"
	updateSQL = "UPDATE bench_rows SET name = $1, email = $2 WHERE id = $3 RETURNING id"
	upsertSQL = "INSERT INTO bench_rows (id, name, email, created_at, birthday, ttl) " +
		"VALUES ($1, $2, $3, $4, $5, $6) " +
		"ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, email = EXCLUDED.email, " +
		"created_at = EXCLUDED.created_at, birthday = EXCLUDED.birthday, ttl = EXCLUDED.ttl " +
		"RETURNING id"
	deleteSQL = "DELETE FROM bench_rows WHERE id = $1"
	// deleteOldestSQL — строку с минимальным id; SKIP LOCKED разводит конкурентные запросы.
	deleteOldestSQL = "DELETE FROM bench_rows WHERE id = (SELECT id FROM bench_rows " +
		"ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED)"

	spreadRows = 1000
)

var (
	// FixedCreated/FixedBirthday/FixedTTL — те же значения, что в bench/api_bench.py.
	FixedCreated  = time.Date(2026, 1, 15, 12, 0, 0, 0, time.UTC)
	FixedBirthday = time.Date(1990, 1, 15, 0, 0, 0, 0, time.UTC)
	FixedTTL      = Interval{Microseconds: 3600 * 1e6}

	spreadSeq atomic.Uint64
)

// NextSpreadID — «разброс» по строкам: 50 клиентов не сериализуются на одной строке.
func NextSpreadID() int {
	return 1 + int(spreadSeq.Add(1)%spreadRows)
}

// Interval — декодированный `interval`; в JSON отдаётся секундами, как timedelta
// в Python-стенде.
type Interval struct {
	Microseconds int64
}

// ScanInterval реализует бинарный путь pgx.
func (i *Interval) ScanInterval(v pgtype.Interval) error {
	i.Microseconds = v.Microseconds + int64(v.Days)*24*3600*1e6 + int64(v.Months)*30*24*3600*1e6
	return nil
}

// MarshalJSON отдаёт секунды (как msgspec для timedelta).
func (i Interval) MarshalJSON() ([]byte, error) {
	return []byte(strconv.FormatFloat(float64(i.Microseconds)/1e6, 'f', -1, 64)), nil
}

// Value реализует driver.Valuer — pgx кодирует interval через этот путь.
func (i Interval) Value() (driver.Value, error) {
	return fmt.Sprintf("%d microseconds", i.Microseconds), nil
}

// User — строка bench_rows в контракте Python-стенда.
type User struct {
	ID       int       `json:"id"`
	Name     string    `json:"name"`
	Email    string    `json:"email"`
	Created  time.Time `json:"created_at"`
	Birthday time.Time `json:"birthday"`
	TTL      Interval  `json:"ttl"`
}

// RowsResponse — тело GET /rows.
type RowsResponse struct {
	Rows []User `json:"rows"`
}

// IDResponse — тело insert/update/upsert.
type IDResponse struct {
	ID int `json:"id"`
}

// OKResponse — тело delete.
type OKResponse struct {
	OK bool `json:"ok"`
}

// NewUser — тело POST /rows.
type NewUser struct {
	Name  string `json:"name"`
	Email string `json:"email"`
}

// UpsertUser — тело POST /rows/upsert.
type UpsertUser struct {
	ID    int    `json:"id"`
	Name  string `json:"name"`
	Email string `json:"email"`
}

// Store — pgx-пул и операции над bench_rows.
type Store struct {
	pool *pgxpool.Pool
}

// Open создаёт пул pgx.
func Open(ctx context.Context, dsn string, maxConns int) (*Store, error) {
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		return nil, fmt.Errorf("pgxpool.ParseConfig: %w", err)
	}
	cfg.MaxConns = int32(maxConns)
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, fmt.Errorf("pgxpool.NewWithConfig: %w", err)
	}
	return &Store{pool: pool}, nil
}

// Close закрывает пул.
func (s *Store) Close() {
	s.pool.Close()
}

// List возвращает limit строк.
func (s *Store) List(ctx context.Context, limit int) ([]User, error) {
	rows, err := s.pool.Query(ctx, rowsSQL, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	users := make([]User, 0, limit)
	for rows.Next() {
		var u User
		if err := rows.Scan(&u.ID, &u.Name, &u.Email, &u.Created, &u.Birthday, &u.TTL); err != nil {
			return nil, err
		}
		users = append(users, u)
	}
	return users, rows.Err()
}

// Insert добавляет строку с фиксированными датами.
func (s *Store) Insert(ctx context.Context, name, email string) (int, error) {
	var id int
	err := s.pool.QueryRow(ctx, insertSQL, name, email,
		FixedCreated, FixedBirthday, FixedTTL).Scan(&id)
	return id, err
}

// Update меняет name/email по id; 0 — строки нет.
func (s *Store) Update(ctx context.Context, id int, name, email string) int {
	var out int
	if err := s.pool.QueryRow(ctx, updateSQL, name, email, id).Scan(&out); err != nil {
		return 0
	}
	return out
}

// Upsert вставляет или обновляет строку по id.
func (s *Store) Upsert(ctx context.Context, id int, name, email string) (int, error) {
	var out int
	err := s.pool.QueryRow(ctx, upsertSQL, id, name, email,
		FixedCreated, FixedBirthday, FixedTTL).Scan(&out)
	return out, err
}

// Delete удаляет строку по id.
func (s *Store) Delete(ctx context.Context, id int) error {
	_, err := s.pool.Exec(ctx, deleteSQL, id)
	return err
}

// DeleteOldest удаляет строку с минимальным id.
func (s *Store) DeleteOldest(ctx context.Context) error {
	_, err := s.pool.Exec(ctx, deleteOldestSQL)
	return err
}

// Claims — claims стендового HS256-токена.
type Claims struct {
	Role string `json:"role"`
	jwt.RegisteredClaims
}

var jwtSecret = []byte(os.Getenv("BENCH_JWT_SECRET"))

// VerifyBearer проверяет `Authorization: Bearer <HS256>` и возвращает claims.
func VerifyBearer(header string) (*Claims, bool) {
	tokenText, ok := strings.CutPrefix(header, "Bearer ")
	if !ok {
		return nil, false
	}
	claims := &Claims{}
	token, err := jwt.ParseWithClaims(tokenText, claims, func(*jwt.Token) (any, error) {
		return jwtSecret, nil
	}, jwt.WithValidMethods([]string{"HS256"}))
	if err != nil || !token.Valid {
		return nil, false
	}
	return claims, true
}

// ParseLimit разбирает `?limit=` (default 100, как в Python-стенде).
func ParseLimit(raw string) int {
	if raw == "" {
		return 100
	}
	n, err := strconv.Atoi(raw)
	if err != nil {
		return 100
	}
	return n
}
