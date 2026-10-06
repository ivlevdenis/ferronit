// pgx читает одну строку по id (10 колонок, users_bench), 1 клиент.
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"sort"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

const (
	dsn       = "postgres://postgres:postgres@127.0.0.1:5432/postgres"
	selectSQL = "SELECT id, name, email, active, score, age, city, created_at, bio, flags " +
		"FROM users_bench WHERE id = $1"
)

type User struct {
	ID        int
	Name      string
	Email     string
	Active    bool
	Score     int
	Age       int
	City      string
	CreatedAt time.Time
	Bio       string
	Flags     []byte // jsonb
}

func runOnce(ctx context.Context, conn *pgxpool.Conn, requests int) float64 {
	started := time.Now()
	for i := 0; i < requests; i++ {
		var u User
		if err := conn.QueryRow(ctx, selectSQL, 1).Scan(
			&u.ID, &u.Name, &u.Email, &u.Active, &u.Score,
			&u.Age, &u.City, &u.CreatedAt, &u.Bio, &u.Flags,
		); err != nil {
			log.Fatal(err)
		}
		_ = u
	}
	return float64(requests) / time.Since(started).Seconds()
}

func main() {
	clients := flag.Int("clients", 1, "клиентов")
	requests := flag.Int("requests", 20000, "запросов на прогон")
	rounds := flag.Int("rounds", 5, "прогонов для медианы")
	flag.Parse()

	ctx := context.Background()
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		log.Fatal(err)
	}
	cfg.MaxConns = int32(*clients)
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		log.Fatal(err)
	}
	defer pool.Close()

	conn, err := pool.Acquire(ctx)
	if err != nil {
		log.Fatal(err)
	}
	defer conn.Release()

	runOnce(ctx, conn, 1000) // прогрев

	rates := make([]float64, 0, *rounds)
	for r := 0; r < *rounds; r++ {
		rates = append(rates, runOnce(ctx, conn, *requests))
	}
	sort.Float64s(rates)
	fmt.Printf("Go pgx, 1 клиент, 1 строка (id=1):  %6.0f req/s  (медиана из %d)\n",
		rates[len(rates)/2], *rounds)
	fmt.Printf("  прогоны: %v\n", rates)
}
