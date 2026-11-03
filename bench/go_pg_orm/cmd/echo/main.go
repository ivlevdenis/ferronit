// Go HTTP bench: Echo + pgx. Same contract as the Python stand.
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"net/http"
	"strconv"

	"github.com/labstack/echo/v4"

	"ferronit_bench/benchapi"
)

func main() {
	serve := flag.Bool("serve", false, "run HTTP server")
	port := flag.Int("port", 8272, "listen port")
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

	e := echo.New()
	e.HideBanner = true
	e.HidePort = true
	e.HTTPErrorHandler = func(err error, c echo.Context) {
		_ = c.JSON(http.StatusInternalServerError, map[string]string{"error": "internal"})
	}

	e.GET("/ping", func(c echo.Context) error {
		return c.JSON(http.StatusOK, map[string]bool{"ok": true})
	})
	e.GET("/me", func(c echo.Context) error {
		claims, ok := benchapi.VerifyBearer(c.Request().Header.Get("Authorization"))
		if !ok {
			return c.JSON(http.StatusUnauthorized, map[string]string{"error": "unauthorized"})
		}
		return c.JSON(http.StatusOK, map[string]string{"sub": claims.Subject, "role": claims.Role})
	})
	e.GET("/rows", func(c echo.Context) error {
		users, err := st.List(c.Request().Context(), benchapi.ParseLimit(c.QueryParam("limit")))
		if err != nil {
			return c.JSON(http.StatusInternalServerError, map[string]string{"error": "db"})
		}
		return c.JSON(http.StatusOK, benchapi.RowsResponse{Rows: users})
	})
	e.POST("/rows", func(c echo.Context) error {
		var body benchapi.NewUser
		if err := c.Bind(&body); err != nil {
			return c.JSON(http.StatusBadRequest, map[string]string{"error": "bad request"})
		}
		id, err := st.Insert(c.Request().Context(), body.Name, body.Email)
		if err != nil {
			return c.JSON(http.StatusInternalServerError, map[string]string{"error": "db"})
		}
		return c.JSON(http.StatusOK, benchapi.IDResponse{ID: id})
	})
	e.PUT("/rows/any", func(c echo.Context) error {
		var body benchapi.NewUser
		if err := c.Bind(&body); err != nil {
			return c.JSON(http.StatusBadRequest, map[string]string{"error": "bad request"})
		}
		return c.JSON(http.StatusOK, benchapi.IDResponse{
			ID: st.Update(c.Request().Context(), benchapi.NextSpreadID(), body.Name, body.Email),
		})
	})
	e.DELETE("/rows/oldest", func(c echo.Context) error {
		_ = st.DeleteOldest(c.Request().Context())
		return c.JSON(http.StatusOK, benchapi.OKResponse{OK: true})
	})
	e.POST("/rows/upsert", func(c echo.Context) error {
		var body benchapi.UpsertUser
		if err := c.Bind(&body); err != nil {
			return c.JSON(http.StatusBadRequest, map[string]string{"error": "bad request"})
		}
		id, err := st.Upsert(c.Request().Context(), body.ID, body.Name, body.Email)
		if err != nil {
			return c.JSON(http.StatusInternalServerError, map[string]string{"error": "db"})
		}
		return c.JSON(http.StatusOK, benchapi.IDResponse{ID: id})
	})
	e.POST("/rows/upsert-any", func(c echo.Context) error {
		var body benchapi.NewUser
		if err := c.Bind(&body); err != nil {
			return c.JSON(http.StatusBadRequest, map[string]string{"error": "bad request"})
		}
		id, err := st.Upsert(c.Request().Context(), benchapi.NextSpreadID(), body.Name, body.Email)
		if err != nil {
			return c.JSON(http.StatusInternalServerError, map[string]string{"error": "db"})
		}
		return c.JSON(http.StatusOK, benchapi.IDResponse{ID: id})
	})
	e.PUT("/rows/:id", func(c echo.Context) error {
		var body benchapi.NewUser
		if err := c.Bind(&body); err != nil {
			return c.JSON(http.StatusBadRequest, map[string]string{"error": "bad request"})
		}
		id, _ := strconv.Atoi(c.Param("id"))
		return c.JSON(http.StatusOK, benchapi.IDResponse{
			ID: st.Update(c.Request().Context(), id, body.Name, body.Email),
		})
	})
	e.DELETE("/rows/:id", func(c echo.Context) error {
		id, _ := strconv.Atoi(c.Param("id"))
		_ = st.Delete(c.Request().Context(), id)
		return c.JSON(http.StatusOK, benchapi.OKResponse{OK: true})
	})

	log.Fatal(e.Start(fmt.Sprintf("127.0.0.1:%d", *port)))
}
