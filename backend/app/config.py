import os
from pathlib import Path

# Load .env if present
env_file = Path(__file__).resolve().parent.parent / ".env"
if env_file.exists():
    with open(env_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())

PROJECT_NAME = os.getenv("PROJECT_NAME", "SatView")
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))
CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",")
    if origin.strip()
]
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./satview.db")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:4b")

# CelesTrak Configuration
CELESTRAK_BASE_URL = os.getenv(
    "CELESTRAK_BASE_URL", "https://celestrak.org/NORAD/elements/gp.php"
)
CELESTRAK_CACHE_TTL_SECONDS = int(os.getenv("CELESTRAK_CACHE_TTL_SECONDS", "7200"))
CELESTRAK_TIMEOUT_SECONDS = float(os.getenv("CELESTRAK_TIMEOUT_SECONDS", "10.0"))

