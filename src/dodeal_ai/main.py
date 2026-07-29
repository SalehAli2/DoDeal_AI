from fastapi import FastAPI

app = FastAPI(title="DODEAL AI Intelligence Layer")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}