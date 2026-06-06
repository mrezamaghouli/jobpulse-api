import os


def get_env_value(key: str, default: str) -> str:
    return os.getenv(key, default)


def get_postgres_config():
    return {
        "host": get_env_value("POSTGRES_HOST", "localhost"),
        "port": get_env_value("POSTGRES_PORT", "5432"),
        "database": get_env_value("POSTGRES_DB", "jobpulse"),
        "user": get_env_value("POSTGRES_USER", "jobpulse_user"),
        "password": get_env_value("POSTGRES_PASSWORD", "jobpulse_password"),
    }


def get_cors_allowed_origins():
    origins = get_env_value(
        "CORS_ALLOWED_ORIGINS",
        "http://127.0.0.1:5500,http://localhost:5500"
    )

    return [
        origin.strip()
        for origin in origins.split(",")
        if origin.strip()
    ]


def get_job_provider_name():
    return get_env_value("JOB_PROVIDER", "json").lower().strip()


def get_app_port():
    return get_env_value("PORT", "8000")


def get_api_key():
    return get_env_value("API_KEY", "").strip()

def get_rate_limit_enabled():
    value = get_env_value("RATE_LIMIT_ENABLED", "false").lower().strip()
    return value in ["true", "1", "yes", "on"]


def get_rate_limit_max_requests():
    return int(get_env_value("RATE_LIMIT_MAX_REQUESTS", "60"))


def get_rate_limit_window_seconds():
    return int(get_env_value("RATE_LIMIT_WINDOW_SECONDS", "60"))
def get_app_name():
    return get_env_value("APP_NAME", "JobPulse API")


def get_app_version():
    return get_env_value("APP_VERSION", "1.0.0")


def get_app_environment():
    return get_env_value("APP_ENV", "development")

def get_linkedin_browser():
    return get_env_value("LINKEDIN_BROWSER", "chrome").lower().strip()


def get_linkedin_keywords():
    return get_env_value("LINKEDIN_KEYWORDS", "UX Designer").strip()


def get_linkedin_location():
    return get_env_value("LINKEDIN_LOCATION", "Germany").strip()


def get_linkedin_limit():
    return int(get_env_value("LINKEDIN_LIMIT", "10"))