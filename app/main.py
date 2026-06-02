from fastapi import FastAPI

app = FastAPI(
    title="Store Intelligence API",
    description="API for CCTV business intelligence",
    version="1.0.0",
)

@app.get("/")
def health_check():
    return {"status": "ok", "message": "Store Intelligence System is running"}
