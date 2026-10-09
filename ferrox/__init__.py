"""Ferrox — High-performance ASGI web framework with DDD support."""

from ferrox import db
from ferrox.core.app import Ferrox
from ferrox.core.request import Request, UploadedFile
from ferrox.core.response import JSONResponse, Response, StreamingResponse, TextResponse
from ferrox.injection import Header
from ferrox.websocket import WebSocket, WebSocketState

__all__ = [
    "Ferrox",
    "Header",
    "JSONResponse",
    "Request",
    "Response",
    "StreamingResponse",
    "TextResponse",
    "UploadedFile",
    "WebSocket",
    "WebSocketState",
    "db",
]
__version__ = "0.8.1"
