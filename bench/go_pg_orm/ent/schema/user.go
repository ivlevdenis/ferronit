package schema

import (
	"entgo.io/ent"
	"entgo.io/ent/dialect"
	"entgo.io/ent/dialect/entsql"
	"entgo.io/ent/schema"
	"entgo.io/ent/schema/field"
)

// User — одна и та же сущность во всех четырёх режимах (pgx/sqlx/GORM/ent).
// Маппится на таблицу bench_rows (та же, что у Python-стенда); миграции не запускаем.
type User struct {
	ent.Schema
}

func (User) Annotations() []schema.Annotation {
	return []schema.Annotation{entsql.Annotation{Table: "bench_rows"}}
}

func (User) Fields() []ent.Field {
	return []ent.Field{
		field.Int("id").Unique(),
		field.String("name"),
		field.String("email"),
		field.Time("created_at"),
		field.Time("birthday"),
		// interval своего типа в ent нет: колонка читается как текст ("01:00:00"),
		// так её и отдаёт pgx stdlib в database/sql. Миграции не запускаем — тип задан на стенде.
		field.String("ttl").SchemaType(map[string]string{dialect.Postgres: "interval"}),
	}
}
