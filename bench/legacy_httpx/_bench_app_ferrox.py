
from ferrox import Ferrox
app = Ferrox()
for i in range(50):
    exec(f'@app.route("/route{i}")\ndef h{i}(req): return {{"route": {i}}}')
