from app.config import (
    get_postgres_config,
    get_cors_allowed_origins,
    get_job_provider_name,
    get_api_key,
    get_rate_limit_enabled,
    get_rate_limit_max_requests,
    get_rate_limit_window_seconds,
)


def test_get_postgres_config_from_env(monkeypatch):
    monkeypatch.setenv("POSTGRES_HOST", "db")
    monkeypatch.setenv("POSTGRES_PORT", "5432")
    monkeypatch.setenv("POSTGRES_DB", "jobpulse")
    monkeypatch.setenv("POSTGRES_USER", "jobpulse_user")
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret")

    config = get_postgres_config()

    assert config["host"] == "db"
    assert config["port"] == "5432"
    assert config["database"] == "jobpulse"
    assert config["user"] == "jobpulse_user"
    assert config["password"] == "secret"


def test_get_cors_allowed_origins(monkeypatch):
    monkeypatch.setenv(
        "CORS_ALLOWED_ORIGINS",
        "http://127.0.0.1:5500, http://localhost:5500"
    )

    origins = get_cors_allowed_origins()

    assert origins == [
        "http://127.0.0.1:5500",
        "http://localhost:5500"
    ]


def test_get_job_provider_name(monkeypatch):
    monkeypatch.setenv("JOB_PROVIDER", " JSON ")

    assert get_job_provider_name() == "json"


def test_get_api_key(monkeypatch):
    monkeypatch.setenv("API_KEY", " test-key ")

    assert get_api_key() == "test-key"


def test_rate_limit_config(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_MAX_REQUESTS", "10")
    monkeypatch.setenv("RATE_LIMIT_WINDOW_SECONDS", "30")

    assert get_rate_limit_enabled() is True
    assert get_rate_limit_max_requests() == 10
    assert get_rate_limit_window_seconds() == 30