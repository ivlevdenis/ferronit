import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "django_bench.settings")

application = get_asgi_application()

# Django режет тело ответа кусками по 64 КБ (ASGIHandler.chunk_size), и granian
# при многочастном теле переключается на ``transfer-encoding: chunked``. ab -k
# не умеет крупные chunked-тела (рвёт соединения от ~50 КБ). Поднимаем порог,
# чтобы ответ уходил одним ASGI-сообщением с Content-Length (см. views._json).
from django.core.handlers.asgi import ASGIHandler

ASGIHandler.chunk_size = 2**20
