import os

from django.http import JsonResponse

from .models import BenchRow


def rows(request):
    limit = int(os.environ.get("BENCH_ROWS", "100"))
    data = list(BenchRow.objects.all()[:limit].values("id", "name", "email"))
    return JsonResponse({"rows": data})


def ping(request):
    return JsonResponse({"ok": True})
