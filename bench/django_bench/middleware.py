"""JWT-аутентификация для стадии `jwt`: middleware + ``GET /me``.

Middleware — идиоматичный для Django механизм; claims кладутся в
``request.jwt_claims``, view только читает их. Токен — HS256, тот же секрет и
claims, что у остальных стеков стенда (``bench/api_bench.py``).
"""

from __future__ import annotations

import os

import jwt
from django.http import JsonResponse

JWT_SECRET = os.environ.get("BENCH_JWT_SECRET", "bench-secret")


class JWTClaimsMiddleware:
    """Проверяет ``Authorization: Bearer <HS256>`` на ``/me`` и кладёт claims в request."""

    def __init__(self, get_response) -> None:
        self.get_response = get_response

    def __call__(self, request):
        if request.path == "/me":
            header = request.headers.get("Authorization", "")
            claims = None
            if header.startswith("Bearer "):
                try:
                    claims = jwt.decode(header[7:], JWT_SECRET, algorithms=["HS256"])
                except jwt.PyJWTError:
                    claims = None
            if claims is None:
                return JsonResponse({"error": "unauthorized"}, status=401)
            request.jwt_claims = claims
        return self.get_response(request)
