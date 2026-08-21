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


# -----------------------------------------------------------------------------
# Tor outbound routing (experimental, disabled by default -- see
# scripts/tor/tor_client.py and scripts/tor/circuit_manager.py). When
# TOR_ENABLED is false, none of the other TOR_* values are read by any
# collector code path.
# -----------------------------------------------------------------------------
def get_tor_enabled():
    value = get_env_value("TOR_ENABLED", "false").lower().strip()
    return value in ["true", "1", "yes", "on"]


def get_tor_socks_host():
    return get_env_value("TOR_SOCKS_HOST", "127.0.0.1").strip()


def _get_int_env_value(key: str, default: int) -> int:
    raw_value = get_env_value(key, str(default))

    try:
        return int(raw_value)
    except ValueError:
        return default


def get_tor_socks_port():
    return _get_int_env_value("TOR_SOCKS_PORT", 9050)


def get_tor_control_host():
    return get_env_value("TOR_CONTROL_HOST", "127.0.0.1").strip()


def get_tor_control_port():
    return _get_int_env_value("TOR_CONTROL_PORT", 9051)


def get_tor_control_password():
    return get_env_value("TOR_CONTROL_PASSWORD", "")


def get_tor_ip_check_url():
    return get_env_value("TOR_IP_CHECK_URL", "https://check.torproject.org/api/ip").strip()


def get_tor_newnym_min_interval_seconds():
    """Minimum time between NEWNYM signals sent to the same Tor instance,
    enforced across separate process invocations via a persisted
    timestamp (see scripts/tor/circuit_manager.py) -- not merely within
    one connection. Defaults to 10s, matching stem's own built-in
    per-connection courtesy interval (Controller.get_newnym_wait())."""
    return _get_int_env_value("TOR_NEWNYM_MIN_INTERVAL_SECONDS", 10)


def get_tor_newnym_max_wait_seconds():
    """Upper bound on how long request_new_identity() will sleep for an
    active NEWNYM cooldown before giving up and raising a transient
    NewnymCooldownError instead. Never exceeded -- this is what keeps
    the wait bounded rather than an unbounded/blocking sleep."""
    return _get_int_env_value("TOR_NEWNYM_MAX_WAIT_SECONDS", 5)


def get_tor_event_max_rows():
    """Phase 2: bounds tor_circuit_events retention (see
    scripts/tor/circuit_manager.py emit_event()'s synchronous pruning --
    not a background cleanup daemon). Clamped to [100, 100000] regardless
    of the configured value, so a misconfigured env var can neither
    disable retention (unbounded row growth) nor prune events before
    anything has a chance to read them."""
    raw = _get_int_env_value("TOR_EVENT_MAX_ROWS", 1000)
    return max(100, min(raw, 100000))


def get_tor_stale_draining_threshold_seconds():
    """Phase 2: how long a circuit may sit in STATUS_DRAINING before
    observability (scripts/tor/observability.py) reports it as stale --
    almost certainly a rotation that started but never finished (e.g. a
    crash mid-NEWNYM). Purely observational: reaching this threshold
    never auto-recovers the circuit -- only an explicit
    recover_circuit() call does (see scripts/tor/circuit_manager.py)."""
    return _get_int_env_value("TOR_STALE_DRAINING_THRESHOLD_SECONDS", 300)
