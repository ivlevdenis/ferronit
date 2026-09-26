from fastapi import FastAPI
app = FastAPI()
for i in range(1000):
    exec(f'@app.get("/route{i}")\ndef h{i}(): return {{"route": {i}}}')
