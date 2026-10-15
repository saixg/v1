from fastapi.testclient import TestClient

from codeaxis_api.main import app


def test_health():
    assert TestClient(app).get("/health").json() == {"status": "ok"}
