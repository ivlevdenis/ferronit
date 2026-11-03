// Go HTTP bench: bare fasthttp + router + pgx. Same contract as the Python stand.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"log"
	"strconv"

	"github.com/fasthttp/router"
	"github.com/valyala/fasthttp"

	"ferronit_bench/benchapi"
)

func writeJSON(ctx *fasthttp.RequestCtx, status int, v any) {
	data, err := json.Marshal(v)
	if err != nil {
		ctx.SetStatusCode(fasthttp.StatusInternalServerError)
		return
	}
	ctx.SetContentType("application/json")
	ctx.SetStatusCode(status)
	ctx.SetBody(data)
}

func main() {
	serve := flag.Bool("serve", false, "run HTTP server")
	port := flag.Int("port", 8274, "listen port")
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

	r := router.New()
	r.GET("/ping", func(c *fasthttp.RequestCtx) {
		writeJSON(c, fasthttp.StatusOK, map[string]bool{"ok": true})
	})
	r.GET("/me", func(c *fasthttp.RequestCtx) {
		claims, ok := benchapi.VerifyBearer(string(c.Request.Header.Peek("Authorization")))
		if !ok {
			writeJSON(c, fasthttp.StatusUnauthorized, map[string]string{"error": "unauthorized"})
			return
		}
		writeJSON(c, fasthttp.StatusOK, map[string]string{"sub": claims.Subject, "role": claims.Role})
	})
	r.GET("/rows", func(c *fasthttp.RequestCtx) {
		users, err := st.List(ctx, benchapi.ParseLimit(string(c.QueryArgs().Peek("limit"))))
		if err != nil {
			writeJSON(c, fasthttp.StatusInternalServerError, map[string]string{"error": "db"})
			return
		}
		writeJSON(c, fasthttp.StatusOK, benchapi.RowsResponse{Rows: users})
	})
	r.POST("/rows", func(c *fasthttp.RequestCtx) {
		var body benchapi.NewUser
		if err := json.Unmarshal(c.PostBody(), &body); err != nil {
			writeJSON(c, fasthttp.StatusBadRequest, map[string]string{"error": "bad request"})
			return
		}
		id, err := st.Insert(ctx, body.Name, body.Email)
		if err != nil {
			writeJSON(c, fasthttp.StatusInternalServerError, map[string]string{"error": "db"})
			return
		}
		writeJSON(c, fasthttp.StatusOK, benchapi.IDResponse{ID: id})
	})
	r.POST("/rows/{id}", func(c *fasthttp.RequestCtx) {
		idRaw := c.UserValue("id").(string)
		switch idRaw {
		case "upsert":
			var body benchapi.UpsertUser
			if err := json.Unmarshal(c.PostBody(), &body); err != nil {
				writeJSON(c, fasthttp.StatusBadRequest, map[string]string{"error": "bad request"})
				return
			}
			id, err := st.Upsert(ctx, body.ID, body.Name, body.Email)
			if err != nil {
				writeJSON(c, fasthttp.StatusInternalServerError, map[string]string{"error": "db"})
				return
			}
			writeJSON(c, fasthttp.StatusOK, benchapi.IDResponse{ID: id})
		case "upsert-any":
			var body benchapi.NewUser
			if err := json.Unmarshal(c.PostBody(), &body); err != nil {
				writeJSON(c, fasthttp.StatusBadRequest, map[string]string{"error": "bad request"})
				return
			}
			id, err := st.Upsert(ctx, benchapi.NextSpreadID(), body.Name, body.Email)
			if err != nil {
				writeJSON(c, fasthttp.StatusInternalServerError, map[string]string{"error": "db"})
				return
			}
			writeJSON(c, fasthttp.StatusOK, benchapi.IDResponse{ID: id})
		default:
			writeJSON(c, fasthttp.StatusNotFound, map[string]string{"error": "not found"})
		}
	})
	r.PUT("/rows/{id}", func(c *fasthttp.RequestCtx) {
		var body benchapi.NewUser
		if err := json.Unmarshal(c.PostBody(), &body); err != nil {
			writeJSON(c, fasthttp.StatusBadRequest, map[string]string{"error": "bad request"})
			return
		}
		if c.UserValue("id").(string) == "any" {
			writeJSON(c, fasthttp.StatusOK, benchapi.IDResponse{
				ID: st.Update(ctx, benchapi.NextSpreadID(), body.Name, body.Email),
			})
			return
		}
		id, _ := strconv.Atoi(c.UserValue("id").(string))
		writeJSON(c, fasthttp.StatusOK, benchapi.IDResponse{ID: st.Update(ctx, id, body.Name, body.Email)})
	})
	r.DELETE("/rows/{id}", func(c *fasthttp.RequestCtx) {
		if c.UserValue("id").(string) == "oldest" {
			_ = st.DeleteOldest(ctx)
			writeJSON(c, fasthttp.StatusOK, benchapi.OKResponse{OK: true})
			return
		}
		id, _ := strconv.Atoi(c.UserValue("id").(string))
		_ = st.Delete(ctx, id)
		writeJSON(c, fasthttp.StatusOK, benchapi.OKResponse{OK: true})
	})

	log.Fatal(fasthttp.ListenAndServe("127.0.0.1:"+strconv.Itoa(*port), r.Handler))
}
