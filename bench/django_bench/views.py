import json

from django.http import JsonResponse

from .models import BenchRow


def _body(request):
    """Тело запроса как dict — одинаковый с другими фреймворками разбор JSON."""
    return json.loads(request.body or b"{}")


def ping(request):
    return JsonResponse({"ok": True})


def rows(request):
    """GET /rows?limit=N — список; POST /rows — INSERT одной строки."""
    if request.method == "POST":
        data = _body(request)
        row = BenchRow.objects.create(name=data["name"], email=data["email"])
        return JsonResponse({"id": row.id})
    limit = int(request.GET.get("limit", "100"))
    data = list(BenchRow.objects.all()[:limit].values("id", "name", "email"))
    return JsonResponse({"rows": data})


def rows_item(request, row_id):
    """PUT /rows/{id} — UPDATE; DELETE /rows/{id} — DELETE."""
    if request.method == "DELETE":
        BenchRow.objects.filter(id=row_id).delete()
        return JsonResponse({"ok": True})
    data = _body(request)
    updated = BenchRow.objects.filter(id=row_id).update(
        name=data["name"], email=data["email"]
    )
    return JsonResponse({"id": row_id if updated else None})


def upsert(request):
    data = _body(request)
    row, _ = BenchRow.objects.update_or_create(
        id=data["id"], defaults={"name": data["name"], "email": data["email"]}
    )
    return JsonResponse({"id": row.id})
