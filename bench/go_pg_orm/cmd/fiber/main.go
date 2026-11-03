// Go HTTP bench: Fiber (fasthttp) + pgx. Same contract as the Python stand.
package main

import (
	"context"
	"flag"
	"log"
	"strconv"

	"github.com/gofiber/fiber/v2"

	"ferronit_bench/benchapi"
)

func main() {
	serve := flag.Bool("serve", false, "run HTTP server")
	port := flag.Int("port", 8273, "listen port")
	poolSize := flag.Int("pool", 16, "pgx pool size")
	flag.Parse()
	if !*serve {
		log.Fatal("--serve is required")
	}
	ctx := context.Background()
	st, err := benchapi.Open(ctx, benchapi.DSN, *poolSize)
	if err != nil {
		log.Fatal(err)
	}
	defer st.Close()

	app := fiber.New(fiber.Config{DisableStartupMessage: true})

	app.Get("/ping", func(c *fiber.Ctx) error {
		return c.JSON(fiber.Map{"ok": true})
	})
	app.Get("/me", func(c *fiber.Ctx) error {
		claims, ok := benchapi.VerifyBearer(c.Get("Authorization"))
		if !ok {
			return c.Status(fiber.StatusUnauthorized).JSON(fiber.Map{"error": "unauthorized"})
		}
		return c.JSON(fiber.Map{"sub": claims.Subject, "role": claims.Role})
	})
	app.Get("/rows", func(c *fiber.Ctx) error {
		users, err := st.List(ctx, benchapi.ParseLimit(c.Query("limit")))
		if err != nil {
			return c.Status(fiber.StatusInternalServerError).JSON(fiber.Map{"error": "db"})
		}
		return c.JSON(benchapi.RowsResponse{Rows: users})
	})
	app.Post("/rows", func(c *fiber.Ctx) error {
		var body benchapi.NewUser
		if err := c.BodyParser(&body); err != nil {
			return c.Status(fiber.StatusBadRequest).JSON(fiber.Map{"error": "bad request"})
		}
		id, err := st.Insert(ctx, body.Name, body.Email)
		if err != nil {
			return c.Status(fiber.StatusInternalServerError).JSON(fiber.Map{"error": "db"})
		}
		return c.JSON(benchapi.IDResponse{ID: id})
	})
	app.Post("/rows/:id", func(c *fiber.Ctx) error {
		switch c.Params("id") {
		case "upsert":
			var body benchapi.UpsertUser
			if err := c.BodyParser(&body); err != nil {
				return c.Status(fiber.StatusBadRequest).JSON(fiber.Map{"error": "bad request"})
			}
			id, err := st.Upsert(ctx, body.ID, body.Name, body.Email)
			if err != nil {
				return c.Status(fiber.StatusInternalServerError).JSON(fiber.Map{"error": "db"})
			}
			return c.JSON(benchapi.IDResponse{ID: id})
		case "upsert-any":
			var body benchapi.NewUser
			if err := c.BodyParser(&body); err != nil {
				return c.Status(fiber.StatusBadRequest).JSON(fiber.Map{"error": "bad request"})
			}
			id, err := st.Upsert(ctx, benchapi.NextSpreadID(), body.Name, body.Email)
			if err != nil {
				return c.Status(fiber.StatusInternalServerError).JSON(fiber.Map{"error": "db"})
			}
			return c.JSON(benchapi.IDResponse{ID: id})
		default:
			return c.Status(fiber.StatusNotFound).JSON(fiber.Map{"error": "not found"})
		}
	})
	app.Put("/rows/:id", func(c *fiber.Ctx) error {
		var body benchapi.NewUser
		if err := c.BodyParser(&body); err != nil {
			return c.Status(fiber.StatusBadRequest).JSON(fiber.Map{"error": "bad request"})
		}
		if c.Params("id") == "any" {
			return c.JSON(benchapi.IDResponse{
				ID: st.Update(ctx, benchapi.NextSpreadID(), body.Name, body.Email),
			})
		}
		id, _ := strconv.Atoi(c.Params("id"))
		return c.JSON(benchapi.IDResponse{ID: st.Update(ctx, id, body.Name, body.Email)})
	})
	app.Delete("/rows/:id", func(c *fiber.Ctx) error {
		if c.Params("id") == "oldest" {
			_ = st.DeleteOldest(ctx)
			return c.JSON(benchapi.OKResponse{OK: true})
		}
		id, _ := strconv.Atoi(c.Params("id"))
		_ = st.Delete(ctx, id)
		return c.JSON(benchapi.OKResponse{OK: true})
	})

	log.Fatal(app.Listen("127.0.0.1:" + strconv.Itoa(*port)))
}
