"""Settings для стенда Django (одна таблица bench_rows в PostgreSQL)."""

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
