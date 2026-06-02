from fastapi import FastAPI

app = FastAPI(
    title="Store Intelligence System",
    description="API for converting raw CCTV footage into business intelligence",
    version="1.0.0",
)

@app.get("/")
async def root():
    return {"message": "Store Intelligence System API"}

@app.get("/health")
async def health_check():
    return {"status": "ok"}
