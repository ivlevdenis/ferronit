from django.http import JsonResponse

from .models import BenchRow


def ping(request):
    return JsonResponse({"ok": True})


def rows(request):
    """GET /rows?limit=N — список; POST /rows — INSERT одной строки."""
    if request.method == "POST":
        row = BenchRow.objects.create(name="bench", email="bench@example.com")
        return JsonResponse({"id": row.id})
    limit = int(request.GET.get("limit", "100"))
    data = list(BenchRow.objects.all()[:limit].values("id", "name", "email"))
    return JsonResponse({"rows": data})


def rows_item(request, row_id):
    """PUT /rows/{id} — UPDATE; DELETE /rows/{id} — DELETE."""
    if request.method == "DELETE":
        BenchRow.objects.filter(id=row_id).delete()
        return JsonResponse({"ok": True})
    updated = BenchRow.objects.filter(id=row_id).update(
        name="bench", email="bench@example.com"
    )
    return JsonResponse({"id": row_id if updated else None})


def upsert(request):
    row, _ = BenchRow.objects.update_or_create(
        id=1, defaults={"name": "bench", "email": "bench@example.com"}
    )
    return JsonResponse({"id": row.id})
