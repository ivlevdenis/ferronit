
from velox import Velox
app = Velox()
for i in range(50):
    def _make(i):
        def h(req, i=i):
            return {"route": i}
        app.route(f"/route{i}")(h)
    _make(i)
