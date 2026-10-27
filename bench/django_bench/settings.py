"""Settings для стенда Django (одна таблица bench_rows в PostgreSQL)."""

import os

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "postgres",
        "USER": "postgres",
        "PASSWORD": "postgres",
        "HOST": "127.0.0.1",
        "PORT": "5432",
    }
}
INSTALLED_APPS = ["django_bench"]
ROOT_URLCONF = "django_bench.urls"
SECRET_KEY = "bench"
DEBUG = False
ALLOWED_HOSTS = ["*"]
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"
USE_TZ = False

# Django 6 по умолчанию имеет пустой MIDDLEWARE; access-log middleware добавляется
# только бенчем логирования (BENCH_ACCESS_LOG=1), обычный стенд остаётся без него.
MIDDLEWARE = []
if os.environ.get("BENCH_ACCESS_LOG") == "1":
    MIDDLEWARE.append("django_bench.logging.AccessLogMiddleware")
