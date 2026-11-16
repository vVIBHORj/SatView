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
- **Orbit Engine**: Skyfield + SGP4 / SDP4 (CelesTrak OMM, GCRS propagation, topocentric observer coordinates, pass prediction)
- **Imagery Integrations** *(future phase)*: STAC API / Copernicus / Microsoft Planetary Computer

---

## Phase 1: Orbit Engine

The Orbit Engine (`backend/app/orbit`) provides high-fidelity astrodynamics capabilities:
- **Task 1.1 — CelesTrak OMM Adapter**: Real-time retrieval, schema validation, and TTL caching for CCSDS OMM JSON orbital data.
- **Task 1.2 — SGP4 Ephemeris Propagation**: GCRS inertial $[X, Y, Z]$ and $[V_X, V_Y, V_Z]$ state vectors computed using SGP4/SDP4 with WGS-72 gravitational model.
- **Task 1.3 — Topocentric Observer Geometry**: WGS84 observer model computing true geometric azimuth, elevation, slant range, range-rate, and relative vectors without atmospheric distortion.
- **Task 1.4 & 1.4.1 — Satellite Pass Prediction**: Multi-pass visibility prediction with Brent's method root-finding for exact AOS/LOS boundary refinement, Golden Section culmination search, and culmination-anchored bracketing for near-tangent grazing passes.

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
- Orbit Topocentric Observation: `http://127.0.0.1:8000/api/orbit/satellite/25544/observe?lat=28.6139&lon=77.2090&alt_m=216.0`
- Orbit Pass Prediction: `http://127.0.0.1:8000/api/orbit/satellite/25544/passes?lat=28.6139&lon=77.2090&min_elevation=10.0&days=1.0`
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
