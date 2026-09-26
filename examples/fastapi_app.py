"""FastAPI equivalent for comparison."""
from fastapi import FastAPI, Request
from pydantic import BaseModel
import time

app = FastAPI()


class HelloReply(BaseModel):
    message: str
    timestamp: float


@app.get("/")
def home():
    return {"ok": True, "framework": "FastAPI"}


@app.get("/hello")
def hello(name: str = "World"):
    return HelloReply(message=f"Hello, {name}!", timestamp=time.time())


@app.get("/reflect")
def reflect(r: Request):
    return {"method": r.method, "path": r.url.path, "headers": dict(r.headers)}
