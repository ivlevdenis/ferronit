// Прямой замер слоёв доступа к PostgreSQL на Go: pgx (драйвер), sqlx, GORM.
// Плюс HTTP-сервис с теми же маршрутами, что у Ferronit (Python) и axum (Rust) в этом стенде.
//
// Замеры слоёв (GOMAXPROCS=1 — аналог одного потока/процесса):
//   GOMAXPROCS=1 ./go_pg_bench --mode gorm --op select --clients 10 --requests 1000
//
// HTTP-сервис:
//   GOMAXPROCS=1 ./go_pg_bench --serve --mode pgx --port 8301 --pool 16
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
	"sync"
	"time"

	"github.com/bytedance/sonic"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/jmoiron/sqlx"
	"entgo.io/ent/dialect"
	entsql "entgo.io/ent/dialect/sql"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"

	"ferronit_bench/ent"

	_ "github.com/jackc/pgx/v5/stdlib" // драйвер для database/sql, sqlx и ent
)

// fastJSON включает быстрый энкодер (sonic) вместо encoding/json — для проверки
// гипотезы, что чтение в Go-сервисе упирается в reflection стандартного кодировщика.
var fastJSON bool

const (
	dsn       = "postgres://postgres:postgres@127.0.0.1:5432/postgres"
	selectSQL = "SELECT id, name, email FROM users LIMIT 100"
	insertSQL = "INSERT INTO users (name, email) VALUES ($1, $2) RETURNING id, name"
)

// User — одна и та же структура для pgx, sqlx и GORM (аналог dict/модели в Python-стенде).
type User struct {
	ID    int    `db:"id" json:"id"`
	Name  string `db:"name" json:"name"`
	Email string `db:"email" json:"email"`
}

type usersResponse struct {
	Users []User `json:"users"`
}

type createdResponse struct {
	ID   int    `json:"id"`
	Name string `json:"name"`
}

type newUser struct {
	Name  string `json:"name"`
	Email string `json:"email"`
}

// store держит ресурсы одного режима: пул pgx, хендл sqlx, GORM или ent.
type store struct {
	pgPool    *pgxpool.Pool
	sqlxDB    *sqlx.DB
	gormDB    *gorm.DB
	entClient *ent.Client
}

func openStore(ctx context.Context, mode string, maxConns int) *store {
	s := &store{}
	switch mode {
	case "pgx":
		cfg, err := pgxpool.ParseConfig(dsn)
		must(err, "разбор DSN")
		cfg.MaxConns = int32(maxConns) // задаём ДО создания пула, иначе не применится
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
				// одно соединение на клиента, как в asyncpg-пуле Python-стенда
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

func serveHTTP(mode string, port, pool int) {
	ctx := context.Background()
	st := openStore(ctx, mode, pool)
	defer st.close()

	mux := http.NewServeMux()
	mux.HandleFunc("/ping", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, map[string]bool{"ok": true})
	})
	mux.HandleFunc("/users", func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodGet:
			var users []User
			switch {
			case st.pgPool != nil:
				rows, err := st.pgPool.Query(r.Context(), selectSQL)
				must(err, "select pgx")
				users = make([]User, 0, 100)
				for rows.Next() {
					var u User
					must(rows.Scan(&u.ID, &u.Name, &u.Email), "scan pgx")
					users = append(users, u)
				}
				rows.Close()
			case st.sqlxDB != nil:
				must(st.sqlxDB.SelectContext(r.Context(), &users, selectSQL), "select sqlx")
			case st.entClient != nil:
				rows, err := st.entClient.User.Query().Limit(100).All(r.Context())
				must(err, "select ent")
				users = make([]User, 0, len(rows))
				for _, u := range rows {
					users = append(users, User{ID: u.ID, Name: u.Name, Email: u.Email})
				}
			default:
				must(st.gormDB.WithContext(r.Context()).Limit(100).Find(&users).Error, "select gorm")
			}
			writeJSON(w, usersResponse{Users: users})
		case http.MethodPost:
			var body newUser
			must(json.NewDecoder(r.Body).Decode(&body), "разбор тела")
			switch {
			case st.pgPool != nil:
				var created createdResponse
				must(st.pgPool.QueryRow(r.Context(), insertSQL, body.Name, body.Email).Scan(&created.ID, &created.Name), "insert pgx")
				writeJSON(w, created)
			case st.sqlxDB != nil:
				var created createdResponse
				must(st.sqlxDB.QueryRowxContext(r.Context(), insertSQL, body.Name, body.Email).Scan(&created.ID, &created.Name), "insert sqlx")
				writeJSON(w, created)
			case st.entClient != nil:
				u, err := st.entClient.User.Create().SetName(body.Name).SetEmail(body.Email).Save(r.Context())
				must(err, "insert ent")
				writeJSON(w, createdResponse{ID: u.ID, Name: u.Name})
			default:
				u := User{Name: body.Name, Email: body.Email}
				must(st.gormDB.WithContext(r.Context()).Create(&u).Error, "insert gorm")
				writeJSON(w, createdResponse{ID: u.ID, Name: u.Name})
			}
		default:
			w.WriteHeader(http.StatusMethodNotAllowed)
		}
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
