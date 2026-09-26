from velox import Velox
app = Velox()
for i in range(1000):
    exec(f'@app.route("/route{i}")\ndef h{i}(req): return {{"route": {i}}}')
