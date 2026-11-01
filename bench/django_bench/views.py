import itertools
import json
from datetime import date, datetime, timedelta

from django.http import JsonResponse

from .models import BenchRow

# USE_TZ=False → datetime без таймзоны; значение фиксировано для всех прогонов.
# «Разброс» по строкам: ab всегда шлёт один и тот же body, поэтому id выбирает сервер.
SPREAD_ROWS = 1000
_spread = itertools.count()

CREATED_AT = datetime(2026, 1, 15, 12, 0, 0)
BIRTHDAY = date(1990, 1, 15)
TTL = timedelta(hours=1)


def _body(request):
    """Тело запроса как dict — одинаковый с другими фреймворками разбор JSON."""
    return json.loads(request.body or b"{}")


def _json(data):
    """JsonResponse с явным Content-Length.

    ASGI-обработчик Django не выставляет Content-Length (это делает только
    WSGI-обработчик), поэтому granian отдаёт тело как ``transfer-encoding:
    chunked``. А ``ab -k`` на крупном chunked-теле рвёт соединения. Явный
    заголовок возвращает ответ в фиксированную длину и чинит keep-alive.
    """
    response = JsonResponse(data)
    response["content-length"] = str(len(response.content))
    return response


def ping(request):
    return _json({"ok": True})


def rows(request):
    """GET /rows?limit=N — список; POST /rows — INSERT одной строки."""
    if request.method == "POST":
        data = _body(request)
        row = BenchRow.objects.create(
            name=data["name"], email=data["email"],
            created_at=CREATED_AT, birthday=BIRTHDAY, ttl=TTL,
        )
        return _json({"id": row.id})
    limit = int(request.GET.get("limit", "100"))
    data = list(
        BenchRow.objects.all()[:limit].values(
            "id", "name", "email", "created_at", "birthday", "ttl"
        )
    )
    return _json({"rows": data})


def rows_item(request, row_id):
    """PUT /rows/{id} — UPDATE; DELETE /rows/{id} — DELETE."""
    if request.method == "DELETE":
        BenchRow.objects.filter(id=row_id).delete()
        return _json({"ok": True})
    data = _body(request)
    updated = BenchRow.objects.filter(id=row_id).update(
        name=data["name"], email=data["email"]
    )
    return _json({"id": row_id if updated else None})


def rows_any(request):
    """PUT /rows/any — UPDATE «размазанной» строки (id выбирает сервер, не клиент)."""
    data = _body(request)
    row_id = 1 + next(_spread) % SPREAD_ROWS
    updated = BenchRow.objects.filter(id=row_id).update(
        name=data["name"], email=data["email"]
    )
    return _json({"id": row_id if updated else None})


def upsert_any(request):
    """POST /rows/upsert-any — UPSERT «размазанной» строки."""
    data = _body(request)
    row_id = 1 + next(_spread) % SPREAD_ROWS
    BenchRow.objects.update_or_create(
        id=row_id,
        defaults={
            "name": data["name"], "email": data["email"],
            "created_at": CREATED_AT, "birthday": BIRTHDAY, "ttl": TTL,
        },
    )
    return _json({"id": row_id})


def delete_oldest(request):
    """DELETE /rows/oldest — реальное удаление: ab бьёт один URL, значит берём min(id).

    Один оператор с ``SKIP LOCKED`` (у Django ORM нет такого API) — чтобы 50
    конкурентных запросов разбирали разные строки, а не ждали одну и ту же.
    """
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM bench_rows WHERE id = (SELECT id FROM bench_rows "
            "ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED)"
        )
    return _json({"ok": True})


def upsert(request):
    data = _body(request)
    row, _ = BenchRow.objects.update_or_create(
        id=data["id"],
        defaults={
            "name": data["name"], "email": data["email"],
            "created_at": CREATED_AT, "birthday": BIRTHDAY, "ttl": TTL,
        },
    )
    return _json({"id": row.id})
