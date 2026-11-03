// Go HTTP bench: chi router + pgx. Same contract as the Python stand.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"strconv"

	"github.com/go-chi/chi/v5"

	"ferronit_bench/benchapi"
)

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func badRequest(w http.ResponseWriter) {
	writeJSON(w, http.StatusBadRequest, map[string]string{"error": "bad request"})
}

func main() {
	serve := flag.Bool("serve", false, "run HTTP server")
	port := flag.Int("port", 8271, "listen port")
	poolSize := flag.Int("pool", 16, "pgx pool size")
	flag.Parse()
	if !*serve {
		log.Fatal("--serve is required")
	}
	st, err := benchapi.Open(context.Background(), benchapi.DSN, *poolSize)
	if err != nil {
		log.Fatal(err)
	}
	defer st.Close()

	r := chi.NewRouter()
	r.Get("/ping", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
	})
	r.Get("/me", func(w http.ResponseWriter, req *http.Request) {
		claims, ok := benchapi.VerifyBearer(req.Header.Get("Authorization"))
		if !ok {
			writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "unauthorized"})
			return
		}
		writeJSON(w, http.StatusOK, map[string]string{"sub": claims.Subject, "role": claims.Role})
	})
	r.Get("/rows", func(w http.ResponseWriter, req *http.Request) {
		users, err := st.List(req.Context(), benchapi.ParseLimit(req.URL.Query().Get("limit")))
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "db"})
			return
		}
		writeJSON(w, http.StatusOK, benchapi.RowsResponse{Rows: users})
	})
	r.Post("/rows", func(w http.ResponseWriter, req *http.Request) {
		var body benchapi.NewUser
		if err := json.NewDecoder(req.Body).Decode(&body); err != nil {
			badRequest(w)
			return
		}
		id, err := st.Insert(req.Context(), body.Name, body.Email)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "db"})
			return
		}
		writeJSON(w, http.StatusOK, benchapi.IDResponse{ID: id})
	})
	r.Put("/rows/any", func(w http.ResponseWriter, req *http.Request) {
		var body benchapi.NewUser
		if err := json.NewDecoder(req.Body).Decode(&body); err != nil {
			badRequest(w)
			return
		}
		writeJSON(w, http.StatusOK, benchapi.IDResponse{
			ID: st.Update(req.Context(), benchapi.NextSpreadID(), body.Name, body.Email),
		})
	})
	r.Delete("/rows/oldest", func(w http.ResponseWriter, req *http.Request) {
		_ = st.DeleteOldest(req.Context())
		writeJSON(w, http.StatusOK, benchapi.OKResponse{OK: true})
	})
	r.Post("/rows/upsert", func(w http.ResponseWriter, req *http.Request) {
		var body benchapi.UpsertUser
		if err := json.NewDecoder(req.Body).Decode(&body); err != nil {
			badRequest(w)
			return
		}
		id, err := st.Upsert(req.Context(), body.ID, body.Name, body.Email)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "db"})
			return
		}
		writeJSON(w, http.StatusOK, benchapi.IDResponse{ID: id})
	})
	r.Post("/rows/upsert-any", func(w http.ResponseWriter, req *http.Request) {
		var body benchapi.NewUser
		if err := json.NewDecoder(req.Body).Decode(&body); err != nil {
			badRequest(w)
			return
		}
		id, err := st.Upsert(req.Context(), benchapi.NextSpreadID(), body.Name, body.Email)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "db"})
			return
		}
		writeJSON(w, http.StatusOK, benchapi.IDResponse{ID: id})
	})
	r.Put("/rows/{id}", func(w http.ResponseWriter, req *http.Request) {
		var body benchapi.NewUser
		if err := json.NewDecoder(req.Body).Decode(&body); err != nil {
			badRequest(w)
			return
		}
		id, _ := strconv.Atoi(chi.URLParam(req, "id"))
		writeJSON(w, http.StatusOK, benchapi.IDResponse{
			ID: st.Update(req.Context(), id, body.Name, body.Email),
		})
	})
	r.Delete("/rows/{id}", func(w http.ResponseWriter, req *http.Request) {
		id, _ := strconv.Atoi(chi.URLParam(req, "id"))
		_ = st.Delete(req.Context(), id)
		writeJSON(w, http.StatusOK, benchapi.OKResponse{OK: true})
	})

	log.Fatal(http.ListenAndServe(fmt.Sprintf("127.0.0.1:%d", *port), r))
}
