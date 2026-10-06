
from ferrox import Ferrox
app = Ferrox()

SMALL = {"ok": True, "route": 0}
MEDIUM = [
    {
        "id": i,
        "name": f"user_{i}",
        "active": i % 2 == 0,
        "score": i * 1.5,
        "tags": [f"tag_{j}" for j in range(5)],
        "meta": {"level": i % 10, "role": "admin" if i % 3 == 0 else "user"},
    }
    for i in range(50)
]
BIG = [
    {
        "id": i,
        "name": f"user_{i}",
        "active": i % 2 == 0,
        "score": i * 1.5,
        "tags": [f"tag_{j}" for j in range(5)],
        "meta": {"level": i % 10, "role": "admin" if i % 3 == 0 else "user"},
    }
    for i in range(500)
]


@app.route("/small")
def small(req):
    return SMALL

@app.route("/medium")
def medium(req):
    return MEDIUM

@app.route("/big")
def big(req):
    return BIG
