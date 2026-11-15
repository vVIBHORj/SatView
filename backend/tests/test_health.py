from fastapi.testclient import TestClient
from app.main import app
from app import config

client = TestClient(app)


def test_root_endpoint():
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["service"] == "SatView"
    assert data["status"] == "online"
    assert "version" in data


def test_health_check_endpoint():
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert "status" in data
    assert "timestamp" in data
    assert data["database"]["type"] == "sqlite"
    assert "ollama" in data
    assert data["ollama"]["configured_model"] == "qwen3:4b"


def test_configuration():
    assert config.PROJECT_NAME == "SatView"
    assert config.PORT == 8000
    assert "sqlite" in config.DATABASE_URL
    assert config.OLLAMA_MODEL == "qwen3:4b"
