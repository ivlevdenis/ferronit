"""Точка сборки (composition root) — здесь входные адаптеры подключаются к приложению.

Прикладной слой (``examples/app.py``) собирается без HTTP; входной адаптер
монтируется отсюда. Запуск сервера:

    uvicorn examples.main:app
"""

from examples.app import app
from examples.http import HttpOrdersAdapter

app.mount("/", HttpOrdersAdapter())

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, port=8000)
