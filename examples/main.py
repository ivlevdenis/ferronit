"""Composition root — incoming adapters are attached to the application here.

The application layer (``examples/app.py``) is assembled without HTTP; the
incoming adapter is mounted from here. Starting the server:

    uvicorn examples.main:app
"""

from examples.app import app
from examples.http import HttpOrdersAdapter

app.mount("/", HttpOrdersAdapter())

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, port=8000)
