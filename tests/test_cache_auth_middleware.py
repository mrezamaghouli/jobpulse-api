"""
Regression tests for the cache/authentication middleware ordering bug.

SimpleApiCacheMiddleware must be the INNERMOST middleware (closest to the
router) so that public_api_security_middleware, api_key_auth, rate_limit,
and ApiGuardMiddleware always run before a cached response can be served.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import app.api_security as api_security_module
import app.main as main_module

FAKE_SEARCH_RESULT = {
    "results": [
        {
            "id": 1,
            "title": "Engineer",
            "company": "Acme",
            "location": "Remote",
            "remote": True,
            "source": "LinkedIn",
            "job_url": "http://example.com/job/1",
        }
    ],
    "count": 1,
    "page": 1,
    "limit": 10,
    "total_pages": 1,
}

VALID_KEY = "test-valid-key"


@pytest.fixture(autouse=True)
def reset_middleware_state():
    """Force a fresh middleware stack (fresh cache + rate buckets) per test."""
    main_module.rate_limit_store.clear()
    api_security_module._rate_buckets.clear()
    main_module.app.middleware_stack = None
    yield
    main_module.rate_limit_store.clear()
    api_security_module._rate_buckets.clear()
    main_module.app.middleware_stack = None


@pytest.fixture
def client():
    return TestClient(main_module.app)


def _configure_env(monkeypatch, rate_limit_per_minute=60):
    monkeypatch.setenv("JOBPULSE_PUBLIC_API_KEYS", VALID_KEY)
    monkeypatch.setenv("JOBPULSE_RATE_LIMIT_PER_MINUTE", str(rate_limit_per_minute))
    monkeypatch.setenv("API_CACHE_ENABLED", "true")
    monkeypatch.setenv("API_CACHE_TTL_SECONDS", "90")
    monkeypatch.setenv("API_ENABLE_RATE_LIMIT", "true")
    monkeypatch.setenv("API_RATE_LIMIT_PER_MINUTE", str(rate_limit_per_minute))
    monkeypatch.setenv("API_SEARCH_RATE_LIMIT_PER_MINUTE", str(rate_limit_per_minute))


def test_authenticated_request_populates_cache(client, monkeypatch):
    _configure_env(monkeypatch)

    with patch.object(main_module, "search_jobs_from_db", return_value=FAKE_SEARCH_RESULT):
        response = client.get(
            "/jobs/search",
            params={"query": "engineer"},
            headers={"X-API-Key": VALID_KEY},
        )

    assert response.status_code == 200
    assert response.headers["x-jobpulse-cache"] == "MISS"
    assert response.json()["count"] == 1


def test_same_url_without_api_key_is_still_rejected_after_cache_populated(client, monkeypatch):
    _configure_env(monkeypatch)

    with patch.object(main_module, "search_jobs_from_db", return_value=FAKE_SEARCH_RESULT) as mocked_db:
        authed = client.get(
            "/jobs/search",
            params={"query": "engineer"},
            headers={"X-API-Key": VALID_KEY},
        )
        assert authed.status_code == 200
        assert authed.headers["x-jobpulse-cache"] == "MISS"

        # Same exact URL/query, no credentials at all.
        unauthed = client.get("/jobs/search", params={"query": "engineer"})

    assert unauthed.status_code == 401
    assert "x-jobpulse-cache" not in unauthed.headers
    # The DB layer must only have been reached for the authenticated request.
    assert mocked_db.call_count == 1


def test_cache_hit_is_still_rate_limited(client, monkeypatch):
    # Only one request per minute allowed for this client identity.
    _configure_env(monkeypatch, rate_limit_per_minute=1)

    with patch.object(main_module, "search_jobs_from_db", return_value=FAKE_SEARCH_RESULT):
        first = client.get(
            "/jobs/search",
            params={"query": "engineer"},
            headers={"X-API-Key": VALID_KEY},
        )
        assert first.status_code == 200
        assert first.headers["x-jobpulse-cache"] == "MISS"

        # Same URL -> would be a cache hit, but the rate limit budget is spent.
        second = client.get(
            "/jobs/search",
            params={"query": "engineer"},
            headers={"X-API-Key": VALID_KEY},
        )

    assert second.status_code == 429
    assert "x-jobpulse-cache" not in second.headers


def test_authorized_cache_hit_returns_expected_response(client, monkeypatch):
    _configure_env(monkeypatch)

    with patch.object(main_module, "search_jobs_from_db", return_value=FAKE_SEARCH_RESULT) as mocked_db:
        first = client.get(
            "/jobs/search",
            params={"query": "engineer"},
            headers={"X-API-Key": VALID_KEY},
        )
        second = client.get(
            "/jobs/search",
            params={"query": "engineer"},
            headers={"X-API-Key": VALID_KEY},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.headers["x-jobpulse-cache"] == "HIT"
    assert first.json() == second.json()
    # DB should only be hit once; the second call was served from cache.
    assert mocked_db.call_count == 1


def test_admin_endpoints_are_never_cached(client, monkeypatch):
    _configure_env(monkeypatch)

    # No ADMIN_API_KEY / JOBPULSE_ADMIN_TOKEN configured -> fails closed with
    # 503 before ever touching the database, so this is safe to call directly.
    first = client.get("/api/admin/summary")
    second = client.get("/api/admin/summary")

    assert first.status_code == 503
    assert second.status_code == 503
    assert "x-jobpulse-cache" not in first.headers
    assert "x-jobpulse-cache" not in second.headers


def test_is_cacheable_excludes_all_registered_admin_routes(monkeypatch):
    """Defense-in-depth: assert the cache allowlist can never match a real
    admin route, using the app's actual registered route table."""
    _configure_env(monkeypatch)

    cache_middleware = main_module.SimpleApiCacheMiddleware(app=None)

    admin_paths = {
        route.path
        for route in main_module.app.routes
        if getattr(route, "path", "").startswith(("/admin", "/api/admin"))
    }
    assert admin_paths, "expected at least one registered admin route"

    class _FakeURL:
        def __init__(self, path):
            self.path = path
            self.query = ""

    class _FakeRequest:
        def __init__(self, path):
            self.method = "GET"
            self.url = _FakeURL(path)

    for path in admin_paths:
        assert cache_middleware._is_cacheable(_FakeRequest(path)) is False

    for path in ("/jobs", "/jobs/search"):
        assert cache_middleware._is_cacheable(_FakeRequest(path)) is True
