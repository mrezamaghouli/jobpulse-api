"""
Regression tests for the admin API routing mismatch fix.

app/admin_api.py's router used to be mounted only at /admin, which nginx never
proxies (it only forwards /api/admin/*), so those endpoints were unreachable
in production. The router is now mounted exclusively at /api/admin (the
canonical convention already used by app/admin_status.py and documented in
docs/API_READINESS.md); the old bare /admin/* path is no longer registered.
"""
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import app.admin_api as admin_api_module
import app.admin_status as admin_status_module
import app.main as main_module

VALID_ADMIN_KEY = "test-admin-key"
VALID_ADMIN_TOKEN = "test-admin-token"

# Every admin_api.py endpoint, keyed by its relative (unprefixed) path.
ADMIN_API_RELATIVE_PATHS = [
    "/summary",
    "/collection-cycles",
    "/demand-queue",
    "/search-events",
    "/jobs-health",
]


@pytest.fixture
def client():
    return TestClient(main_module.app)


@pytest.fixture
def fake_pg_connection():
    """A context-manager-friendly mock standing in for get_postgres_connection()."""
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = {"total_jobs": 1}
    cursor.fetchall.return_value = []
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    conn.cursor.return_value.__enter__.return_value = cursor
    conn.cursor.return_value.__exit__.return_value = False
    return conn


@pytest.fixture
def admin_key_configured(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", VALID_ADMIN_KEY)


@pytest.fixture
def admin_token_configured(monkeypatch):
    # ADMIN_TOKEN is read once at import time in app/admin_status.py, so
    # setenv alone would not affect an already-imported module.
    monkeypatch.setattr(admin_status_module, "ADMIN_TOKEN", VALID_ADMIN_TOKEN)


@pytest.mark.parametrize("relative_path", ADMIN_API_RELATIVE_PATHS)
def test_api_admin_route_is_registered(client, admin_key_configured, relative_path):
    """/api/admin/* must be a real, auth-gated route (401), never 404."""
    response = client.get(f"/api/admin{relative_path}")
    assert response.status_code == 401
    assert response.status_code != 404


def test_bare_admin_summary_no_longer_registered(client, admin_key_configured):
    """The old /admin/* convention must not remain reachable even with a
    valid key: it's not a supported route, so it must 404."""
    response = client.get("/admin/summary", headers={"X-Admin-Key": VALID_ADMIN_KEY})
    assert response.status_code == 404


def test_missing_admin_api_key_config_fails_closed(client, monkeypatch):
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    response = client.get("/api/admin/summary", headers={"X-Admin-Key": "anything"})
    assert response.status_code == 503


def test_invalid_admin_key_rejected(client, admin_key_configured):
    response = client.get("/api/admin/summary", headers={"X-Admin-Key": "wrong-key"})
    assert response.status_code == 401


def test_valid_admin_key_reaches_summary_handler(client, admin_key_configured, fake_pg_connection):
    with patch.object(admin_api_module, "get_postgres_connection", return_value=fake_pg_connection):
        response = client.get(
            "/api/admin/summary",
            headers={"X-Admin-Key": VALID_ADMIN_KEY},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.parametrize(
    "relative_path",
    ["/collection-cycles", "/demand-queue", "/search-events", "/jobs-health"],
)
def test_valid_admin_key_reaches_other_handlers(
    client, admin_key_configured, fake_pg_connection, relative_path
):
    with patch.object(admin_api_module, "get_postgres_connection", return_value=fake_pg_connection):
        response = client.get(
            f"/api/admin{relative_path}",
            headers={"X-Admin-Key": VALID_ADMIN_KEY},
        )

    assert response.status_code == 200


def test_api_admin_status_still_registered(client, admin_token_configured):
    """The already-working admin_status.py routes must be untouched by this fix."""
    response = client.get("/api/admin/status", headers={"X-Admin-Token": "wrong"})
    assert response.status_code == 401
    assert response.status_code != 404


def test_api_admin_action_still_registered(client, admin_token_configured):
    response = client.post("/api/admin/action", headers={"X-Admin-Token": "wrong"})
    assert response.status_code == 401
    assert response.status_code != 404


def test_api_admin_logs_still_registered(client, admin_token_configured):
    response = client.get("/api/admin/logs", headers={"X-Admin-Token": "wrong"})
    assert response.status_code == 401
    assert response.status_code != 404


def test_nginx_proxies_api_admin_to_backend_api_admin():
    """The nginx proxy target must match the FastAPI /api/admin/* convention:
    the /api/admin/ prefix must be preserved end-to-end, not stripped."""
    nginx_conf = __import__("pathlib").Path("frontend/nginx.conf").read_text()

    assert "location ^~ /api/admin/" in nginx_conf
    assert "proxy_pass http://api:8000/api/admin/;" in nginx_conf


@pytest.mark.parametrize(
    "path",
    [
        "/api/admin/summary",
        "/api/admin/collection-cycles",
        "/api/admin/demand-queue",
        "/api/admin/search-events",
        "/api/admin/jobs-health",
        "/api/admin/status",
        "/api/admin/action",
        "/api/admin/logs",
    ],
)
def test_no_admin_route_exposed_without_credential(
    client, admin_key_configured, admin_token_configured, path
):
    """No admin route may ever return 200 to a request carrying no credential."""
    if path in ("/api/admin/action",):
        response = client.post(path)
    else:
        response = client.get(path)

    assert response.status_code in (401, 503)
