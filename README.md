# SatView 🛰️

> Satellite tracking and open-imagery discovery platform.

SatView is designed to answer two distinct questions for any specified location and time:
1. **Overhead**: Which satellites passed above the location (orbital mechanics via SGP4).
2. **Photographed**: Which open-data imaging satellites actually captured imagery covering that spot (Copernicus Sentinel, USGS/NASA Landsat, NASA GIBS, ISRO MOSDAC).

---

## Architecture

- **Backend**: Python 3.12 + FastAPI
- **Frontend**: React 18 + Vite
- **Database**: SQLite (`satview.db`)
- **AI Explainer**: Local Ollama (`qwen3:4b` primary model)
- **Orbit Engine** *(future phase)*: Skyfield + SGP4
- **Imagery Integrations** *(future phase)*: STAC API / Copernicus / Microsoft Planetary Computer

---

## Phase 0 Foundation

Phase 0 establishes the base environment, project configuration, and verified runtime connectivity.

### Prerequisites

- Python 3.12+ (64-bit)
- Node.js v20+ & npm v10+
- Ollama running locally (`http://127.0.0.1:11434`) with `qwen3:4b` pulled
- Git

---

## Quick Start

### 1. Backend Setup

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

- API Base URL: `http://127.0.0.1:8000`
- Interactive API Docs: `http://127.0.0.1:8000/docs`
- Health Check: `http://127.0.0.1:8000/api/health`
- Orbit Satellite Lookup: `http://127.0.0.1:8000/api/orbit/satellite/25544`
- Orbit SGP4 Propagation: `http://127.0.0.1:8000/api/orbit/satellite/25544/propagate?time=2026-11-15T05:00:00Z`
- Orbit Group Lookup: `http://127.0.0.1:8000/api/orbit/group/stations`



### 2. Frontend Setup

```powershell
cd frontend
npm install
npm run dev
```

- Web UI: `http://127.0.0.1:5173`

---

## Verification & Testing

### Run Backend Tests

```powershell
cd backend
.\.venv\Scripts\pytest -v tests
```

### Run Frontend Build Check

```powershell
cd frontend
npm run build
```
