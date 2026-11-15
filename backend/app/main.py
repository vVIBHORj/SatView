from datetime import datetime, timezone
import sqlite3
import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import (
    CORS_ORIGINS,
    DATABASE_URL,
    OLLAMA_BASE_URL,
    OLLAMA_MODEL,
    PROJECT_NAME,
)

app = FastAPI(
    title=PROJECT_NAME,
    description="Satellite tracking and open-imagery discovery platform API",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def read_root():
    return {
        "service": PROJECT_NAME,
        "version": "0.1.0",
        "status": "online",
        "phase": "Phase 0 - Foundation",
    }


@app.get("/api/health")
async def health_check():
    now = datetime.now(timezone.utc).isoformat()

    # SQLite check
    db_status = "ok"
    try:
        # Extract path from sqlite URL or fallback to memory test
        db_path = (
            DATABASE_URL.replace("sqlite:///", "")
            if DATABASE_URL.startswith("sqlite:///")
            else ":memory:"
        )
        conn = sqlite3.connect(db_path)
        conn.execute("SELECT 1")
        conn.close()
    except Exception as exc:
        db_status = f"error: {exc}"

    # Ollama connectivity check
    ollama_info = {
        "status": "unavailable",
        "model_available": False,
        "configured_model": OLLAMA_MODEL,
    }
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
            if resp.status_code == 200:
                data = resp.json()
                models = [m.get("name") for m in data.get("models", [])]
                ollama_info["status"] = "connected"
                ollama_info["model_available"] = any(
                    OLLAMA_MODEL in m for m in models
                )
    except Exception:
        ollama_info["status"] = "unreachable"

    return {
        "status": "healthy" if db_status == "ok" else "degraded",
        "timestamp": now,
        "database": {"type": "sqlite", "status": db_status},
        "ollama": ollama_info,
    }
