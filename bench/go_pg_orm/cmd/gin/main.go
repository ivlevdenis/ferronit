// Go HTTP bench: Gin + pgx. Same contract as the Python stand.
//
// Gin's router (httprouter-based) panics on static/param siblings, so the
// special paths (/rows/any, /rows/oldest, /rows/upsert, /rows/upsert-any) are
// dispatched inside the :id handler instead of being registered separately.
package main

import (
	"context"
	"flag"
	"log"
	"net/http"
	"strconv"

	"github.com/gin-gonic/gin"

	"ferronit_bench/benchapi"
)

func main() {
	serve := flag.Bool("serve", false, "run HTTP server")
	port := flag.Int("port", 8275, "listen port")
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

	gin.SetMode(gin.ReleaseMode)
	r := gin.New()

	r.GET("/ping", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"ok": true})
	})
	r.GET("/me", func(c *gin.Context) {
		claims, ok := benchapi.VerifyBearer(c.GetHeader("Authorization"))
		if !ok {
			c.JSON(http.StatusUnauthorized, gin.H{"error": "unauthorized"})
			return
		}
		c.JSON(http.StatusOK, gin.H{"sub": claims.Subject, "role": claims.Role})
	})
	r.GET("/rows", func(c *gin.Context) {
		users, err := st.List(c.Request.Context(), benchapi.ParseLimit(c.Query("limit")))
		if err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": "db"})
			return
		}
		c.JSON(http.StatusOK, benchapi.RowsResponse{Rows: users})
	})
	r.POST("/rows", func(c *gin.Context) {
		var body benchapi.NewUser
		if err := c.ShouldBindJSON(&body); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": "bad request"})
			return
		}
		id, err := st.Insert(c.Request.Context(), body.Name, body.Email)
		if err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": "db"})
			return
		}
		c.JSON(http.StatusOK, benchapi.IDResponse{ID: id})
	})
	r.POST("/rows/:id", func(c *gin.Context) {
		switch c.Param("id") {
		case "upsert":
			var body benchapi.UpsertUser
			if err := c.ShouldBindJSON(&body); err != nil {
				c.JSON(http.StatusBadRequest, gin.H{"error": "bad request"})
				return
			}
			id, err := st.Upsert(c.Request.Context(), body.ID, body.Name, body.Email)
			if err != nil {
				c.JSON(http.StatusInternalServerError, gin.H{"error": "db"})
				return
			}
			c.JSON(http.StatusOK, benchapi.IDResponse{ID: id})
		case "upsert-any":
			var body benchapi.NewUser
			if err := c.ShouldBindJSON(&body); err != nil {
				c.JSON(http.StatusBadRequest, gin.H{"error": "bad request"})
				return
			}
			id, err := st.Upsert(c.Request.Context(), benchapi.NextSpreadID(), body.Name, body.Email)
			if err != nil {
				c.JSON(http.StatusInternalServerError, gin.H{"error": "db"})
				return
			}
			c.JSON(http.StatusOK, benchapi.IDResponse{ID: id})
		default:
			c.JSON(http.StatusNotFound, gin.H{"error": "not found"})
		}
	})
	r.PUT("/rows/:id", func(c *gin.Context) {
		var body benchapi.NewUser
		if err := c.ShouldBindJSON(&body); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": "bad request"})
			return
		}
		if c.Param("id") == "any" {
			c.JSON(http.StatusOK, benchapi.IDResponse{
				ID: st.Update(c.Request.Context(), benchapi.NextSpreadID(), body.Name, body.Email),
			})
			return
		}
		id, _ := strconv.Atoi(c.Param("id"))
		c.JSON(http.StatusOK, benchapi.IDResponse{
			ID: st.Update(c.Request.Context(), id, body.Name, body.Email),
		})
	})
	r.DELETE("/rows/:id", func(c *gin.Context) {
		if c.Param("id") == "oldest" {
			_ = st.DeleteOldest(c.Request.Context())
			c.JSON(http.StatusOK, benchapi.OKResponse{OK: true})
			return
		}
		id, _ := strconv.Atoi(c.Param("id"))
		_ = st.Delete(c.Request.Context(), id)
		c.JSON(http.StatusOK, benchapi.OKResponse{OK: true})
	})

	log.Fatal(r.Run("127.0.0.1:" + strconv.Itoa(*port)))
}
