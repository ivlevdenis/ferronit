// Прямой замер слоёв доступа к PostgreSQL на Go: pgx (драйвер), sqlx, GORM.
// Плюс HTTP-сервис на том же контракте, что у api_bench.py (Python-стенд):
//   GET /ping, GET /rows?limit=N, POST /rows, PUT /rows/{id}, DELETE /rows/{id}, POST /rows/upsert.
//
// Таблица — bench_rows (та же, что у Python-стенда), 1000 строк.
package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"runtime"
	"sort"
	"strconv"
	"sync"
	"time"

	"github.com/bytedance/sonic"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/jmoiron/sqlx"
	"entgo.io/ent/dialect"
	entsql "entgo.io/ent/dialect/sql"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/clause"
	"gorm.io/gorm/logger"

	"ferronit_bench/ent"

	_ "github.com/jackc/pgx/v5/stdlib" // драйвер для database/sql, sqlx и ent
)

var fastJSON bool

const (
	dsn       = "postgres://postgres:postgres@127.0.0.1:5432/postgres"
	selectSQL = "SELECT id, name, email FROM bench_rows LIMIT 100" // слои (фикс 100)
	insertSQL = "INSERT INTO bench_rows (name, email) VALUES ($1, $2) RETURNING id, name"
	updateSQL = "UPDATE bench_rows SET name = $1, email = $2 WHERE id = $3 RETURNING id"
	upsertSQL = "INSERT INTO bench_rows (id, name, email) VALUES ($1, $2, $3) " +
		"ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, email = EXCLUDED.email RETURNING id"
	deleteSQL = "DELETE FROM bench_rows WHERE id = $1"
	// rowsSQL — чтение с query-параметром limit (HTTP /rows?limit=N)
	rowsSQL = "SELECT id, name, email FROM bench_rows LIMIT $1"
)

type User struct {
	ID    int    `db:"id" json:"id"`
	Name  string `db:"name" json:"name"`
	Email string `db:"email" json:"email"`
}

// gorm по умолчанию плюрализует User → users; явно ведём на bench_rows,
// ту же таблицу, что у pgx/sqlx/ent и Python-стенда.
func (User) TableName() string { return "bench_rows" }

type rowsResponse struct {
	Rows []User `json:"rows"`
}

type idResponse struct {
	ID int `json:"id"`
}

type okResponse struct {
	OK bool `json:"ok"`
}

type newUser struct {
	Name  string `json:"name"`
	Email string `json:"email"`
}

type upsertUser struct {
	ID    int    `json:"id"`
	Name  string `json:"name"`
	Email string `json:"email"`
}

type store struct {
	pgPool    *pgxpool.Pool
	sqlxDB    *sqlx.DB
	gormDB    *gorm.DB
	entClient *ent.Client
	rawDB     *sql.DB // для ent: тот же database/sql, что под ent-driver
}

func openStore(ctx context.Context, mode string, maxConns int) *store {
	s := &store{}
	switch mode {
	case "pgx":
		cfg, err := pgxpool.ParseConfig(dsn)
		must(err, "разбор DSN")
		cfg.MaxConns = int32(maxConns)
		s.pgPool, err = pgxpool.NewWithConfig(ctx, cfg)
		must(err, "пул pgx")
	case "sqlx":
		var err error
		s.sqlxDB, err = sqlx.Open("pgx", dsn)
		must(err, "sqlx.Open")
		s.sqlxDB.SetMaxOpenConns(maxConns)
	case "gorm":
		var err error
		s.gormDB, err = gorm.Open(postgres.Open(dsn), &gorm.Config{
			Logger: logger.Default.LogMode(logger.Silent),
		})
		must(err, "gorm.Open")
		sqlDB, err := s.gormDB.DB()
		must(err, "gorm DB()")
		sqlDB.SetMaxOpenConns(maxConns)
	case "ent":
		var err error
		sqlDB, err := sql.Open("pgx", dsn)
		must(err, "ent: database/sql")
		sqlDB.SetMaxOpenConns(maxConns)
		s.rawDB = sqlDB
		s.entClient = ent.NewClient(ent.Driver(entsql.OpenDB(dialect.Postgres, sqlDB)))
	default:
		log.Fatalf("неизвестный режим: %s (есть: pgx, sqlx, gorm, ent)", mode)
	}
	return s
}

func (s *store) close() {
	if s.pgPool != nil {
		s.pgPool.Close()
	}
	if s.sqlxDB != nil {
		_ = s.sqlxDB.Close()
	}
	if s.entClient != nil {
		_ = s.entClient.Close()
	}
}

func main() {
	mode := flag.String("mode", "pgx", "pgx | sqlx | gorm | ent")
	op := flag.String("op", "select", "select | insert")
	clients := flag.Int("clients", 10, "одновременных клиентов (горутин)")
	requests := flag.Int("requests", 1000, "запросов на клиента")
	serve := flag.Bool("serve", false, "запустить HTTP-сервис вместо замеров")
	port := flag.Int("port", 8301, "порт для HTTP-сервиса")
	pool := flag.Int("pool", 16, "размер пула соединений")
	jsonMode := flag.String("json", "std", "std | sonic — кодировщик JSON в сервисе")
	flag.Parse()

	fastJSON = *jsonMode == "sonic"

	if *serve {
		serveHTTP(*mode, *port, *pool)
		return
	}
	runBench(*mode, *op, *clients, *requests)
}

// ── замеры слоёв ─────────────────────────────────────────────────────────────

func runBench(mode, op string, clients, requests int) {
	ctx := context.Background()
	st := openStore(ctx, mode, clients)
	defer st.close()

	started := time.Now()
	perClient := make([][]float64, clients)
	var wg sync.WaitGroup

	for c := 0; c < clients; c++ {
		wg.Add(1)
		go func(c int) {
			defer wg.Done()
			local := make([]float64, 0, requests)

			if st.pgPool != nil {
				conn, err := st.pgPool.Acquire(ctx)
				must(err, "соединение pgx")
				defer conn.Release()
				for i := 0; i < requests; i++ {
					t0 := time.Now()
					if op == "insert" {
						var id int
						var name string
						must(conn.QueryRow(ctx, insertSQL, fmt.Sprintf("bench_%d_%d", c, i), "bench@example.com").Scan(&id, &name), "insert pgx")
					} else {
						rows, err := conn.Query(ctx, selectSQL)
						must(err, "select pgx")
						users := make([]User, 0, 100)
						for rows.Next() {
							var u User
							must(rows.Scan(&u.ID, &u.Name, &u.Email), "scan pgx")
							users = append(users, u)
						}
						rows.Close()
						_ = users
					}
					local = append(local, float64(time.Since(t0).Microseconds()))
				}
			} else {
				for i := 0; i < requests; i++ {
					t0 := time.Now()
					switch {
					case st.sqlxDB != nil && op == "insert":
						var id int
						var name string
						must(st.sqlxDB.QueryRowxContext(ctx, insertSQL, fmt.Sprintf("bench_%d_%d", c, i), "bench@example.com").Scan(&id, &name), "insert sqlx")
					case st.sqlxDB != nil:
						var users []User
						must(st.sqlxDB.SelectContext(ctx, &users, selectSQL), "select sqlx")
					case st.entClient != nil && op == "insert":
						_, err := st.entClient.User.Create().
							SetName(fmt.Sprintf("bench_%d_%d", c, i)).
							SetEmail("bench@example.com").
							Save(ctx)
						must(err, "insert ent")
					case st.entClient != nil:
						_, err := st.entClient.User.Query().Limit(100).All(ctx)
						must(err, "select ent")
					case op == "insert":
						u := User{Name: fmt.Sprintf("bench_%d_%d", c, i), Email: "bench@example.com"}
						must(st.gormDB.WithContext(ctx).Create(&u).Error, "insert gorm")
					default:
						var users []User
						must(st.gormDB.WithContext(ctx).Limit(100).Find(&users).Error, "select gorm")
					}
					local = append(local, float64(time.Since(t0).Microseconds()))
				}
			}
			perClient[c] = local
		}(c)
	}
	wg.Wait()

	wall := time.Since(started).Seconds()
	durations := make([]float64, 0, clients*requests)
	for _, local := range perClient {
		durations = append(durations, local...)
	}
	sort.Float64s(durations)
	pct := func(p float64) float64 {
		idx := int(float64(len(durations)) * p)
		if idx >= len(durations) {
			idx = len(durations) - 1
		}
		return durations[idx] / 1000.0
	}
	fmt.Printf(
		"Go   %-5s %-6s GOMAXPROCS=%-2d клиентов=%-3d запросов=%-5d -> %9.0f зап/с   p50 %.2f мс   p95 %.2f мс\n",
		mode, op, runtime.GOMAXPROCS(0), clients, requests, float64(len(durations))/wall, pct(0.50), pct(0.95),
	)
}

// ── HTTP-сервис ──────────────────────────────────────────────────────────────

func (s *store) listUsers(ctx context.Context, limit int) []User {
	switch {
	case s.pgPool != nil:
		rows, err := s.pgPool.Query(ctx, rowsSQL, limit)
		must(err, "select pgx")
		users := make([]User, 0, limit)
		for rows.Next() {
			var u User
			must(rows.Scan(&u.ID, &u.Name, &u.Email), "scan pgx")
			users = append(users, u)
		}
		rows.Close()
		return users
	case s.sqlxDB != nil:
		var users []User
		must(s.sqlxDB.SelectContext(ctx, &users, rowsSQL, limit), "select sqlx")
		return users
	case s.entClient != nil:
		rows, err := s.entClient.User.Query().Limit(limit).All(ctx)
		must(err, "select ent")
		users := make([]User, 0, len(rows))
		for _, u := range rows {
			users = append(users, User{ID: u.ID, Name: u.Name, Email: u.Email})
		}
		return users
	default:
		var users []User
		must(s.gormDB.WithContext(ctx).Limit(limit).Find(&users).Error, "select gorm")
		return users
	}
}

func (s *store) insertUser(ctx context.Context, name, email string) int {
	switch {
	case s.pgPool != nil:
		var id int
		var n string
		must(s.pgPool.QueryRow(ctx, insertSQL, name, email).Scan(&id, &n), "insert pgx")
		return id
	case s.sqlxDB != nil:
		var id int
		var n string
		must(s.sqlxDB.QueryRowxContext(ctx, insertSQL, name, email).Scan(&id, &n), "insert sqlx")
		return id
	case s.entClient != nil:
		u, err := s.entClient.User.Create().SetName(name).SetEmail(email).Save(ctx)
		must(err, "insert ent")
		return u.ID
	default:
		u := User{Name: name, Email: email}
		must(s.gormDB.WithContext(ctx).Create(&u).Error, "insert gorm")
		return u.ID
	}
}

func (s *store) updateUser(ctx context.Context, id int, name, email string) int {
	switch {
	case s.pgPool != nil:
		var out int
		err := s.pgPool.QueryRow(ctx, updateSQL, name, email, id).Scan(&out)
		if err != nil {
			return 0
		}
		return out
	case s.sqlxDB != nil:
		var out int
		if err := s.sqlxDB.QueryRowxContext(ctx, updateSQL, name, email, id).Scan(&out); err != nil {
			return 0
		}
		return out
	case s.entClient != nil:
		_, err := s.entClient.User.UpdateOneID(id).SetName(name).SetEmail(email).Save(ctx)
		if err != nil {
			return 0
		}
		return id
	default:
		res := s.gormDB.WithContext(ctx).Model(&User{}).Where("id = ?", id).
			Updates(map[string]any{"name": name, "email": email})
		if res.RowsAffected == 0 {
			return 0
		}
		return id
	}
}

func (s *store) upsertUser(ctx context.Context, id int, name, email string) int {
	switch {
	case s.pgPool != nil:
		var out int
		must(s.pgPool.QueryRow(ctx, upsertSQL, id, name, email).Scan(&out), "upsert pgx")
		return out
	case s.sqlxDB != nil:
		var out int
		must(s.sqlxDB.QueryRowxContext(ctx, upsertSQL, id, name, email).Scan(&out), "upsert sqlx")
		return out
	case s.entClient != nil:
		var out int
		must(s.rawDB.QueryRowContext(ctx, upsertSQL, id, name, email).Scan(&out), "upsert ent")
		return out
	default:
		u := User{ID: id, Name: name, Email: email}
		err := s.gormDB.WithContext(ctx).Clauses(clause.OnConflict{
			Columns:   []clause.Column{{Name: "id"}},
			DoUpdates: clause.AssignmentColumns([]string{"name", "email"}),
		}).Create(&u).Error
		must(err, "upsert gorm")
		return id
	}
}

func (s *store) deleteUser(ctx context.Context, id int) {
	switch {
	case s.pgPool != nil:
		_, _ = s.pgPool.Exec(ctx, deleteSQL, id)
	case s.sqlxDB != nil:
		_, _ = s.sqlxDB.ExecContext(ctx, deleteSQL, id)
	case s.entClient != nil:
		_ = s.entClient.User.DeleteOneID(id).Exec(ctx)
	default:
		_ = s.gormDB.WithContext(ctx).Delete(&User{}, id).Error
	}
}

func serveHTTP(mode string, port, pool int) {
	ctx := context.Background()
	st := openStore(ctx, mode, pool)
	defer st.close()

	mux := http.NewServeMux()
	mux.HandleFunc("GET /ping", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, map[string]bool{"ok": true})
	})
	mux.HandleFunc("GET /rows", func(w http.ResponseWriter, r *http.Request) {
		limit := 100
		if v := r.URL.Query().Get("limit"); v != "" {
			if n, err := strconv.Atoi(v); err == nil {
				limit = n
			}
		}
		writeJSON(w, rowsResponse{Rows: st.listUsers(r.Context(), limit)})
	})
	mux.HandleFunc("POST /rows", func(w http.ResponseWriter, r *http.Request) {
		var body newUser
		must(json.NewDecoder(r.Body).Decode(&body), "разбор тела")
		writeJSON(w, idResponse{ID: st.insertUser(r.Context(), body.Name, body.Email)})
	})
	mux.HandleFunc("PUT /rows/{id}", func(w http.ResponseWriter, r *http.Request) {
		var body newUser
		must(json.NewDecoder(r.Body).Decode(&body), "разбор тела")
		id, _ := strconv.Atoi(r.PathValue("id"))
		writeJSON(w, idResponse{ID: st.updateUser(r.Context(), id, body.Name, body.Email)})
	})
	mux.HandleFunc("DELETE /rows/{id}", func(w http.ResponseWriter, r *http.Request) {
		id, _ := strconv.Atoi(r.PathValue("id"))
		st.deleteUser(r.Context(), id)
		writeJSON(w, okResponse{OK: true})
	})
	mux.HandleFunc("POST /rows/upsert", func(w http.ResponseWriter, r *http.Request) {
		var body upsertUser
		must(json.NewDecoder(r.Body).Decode(&body), "разбор тела")
		writeJSON(w, idResponse{ID: st.upsertUser(r.Context(), body.ID, body.Name, body.Email)})
	})

	fmt.Printf("go_pg_bench сервис готов: режим %s, порт %d, пул %d\n", mode, port, pool)
	log.Fatal(http.ListenAndServe(fmt.Sprintf("127.0.0.1:%d", port), mux))
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	if fastJSON {
		data, err := sonic.Marshal(v)
		must(err, "sonic.Marshal")
		_, _ = w.Write(data)
		return
	}
	_ = json.NewEncoder(w).Encode(v)
}

func must(err error, what string) {
	if err != nil {
		log.Fatalf("%s: %v", what, err)
	}
}
