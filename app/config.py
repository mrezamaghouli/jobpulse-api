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


def get_tor_control_password_file():
    """Path to a file containing the plaintext ControlPort password (e.g.
    a Docker/Compose secret mount such as /run/secrets/tor_control_password).
    Preferred over TOR_CONTROL_PASSWORD in production: only a FILE PATH
    (never the secret itself) needs to travel through the container
    environment/`docker inspect`. See get_tor_control_password()."""
    return get_env_value("TOR_CONTROL_PASSWORD_FILE", "").strip()


def get_tor_control_password():
    """Prefers TOR_CONTROL_PASSWORD_FILE when set (reads and returns its
    contents, trailing whitespace stripped) -- this is the production
    path, and never requires the plaintext password as an environment
    variable value. Falls back to the TOR_CONTROL_PASSWORD environment
    variable (local dev/CI convenience only) when no file is configured.
    Never logs or echoes the resolved value; callers (e.g.
    scripts/tor/circuit_manager.py) must keep the same discipline."""
    password_file = get_tor_control_password_file()

    if password_file:
        try:
            with open(password_file, "r") as file_handle:
                return file_handle.read().strip()
        except OSError as error:
            raise RuntimeError(
                f"TOR_CONTROL_PASSWORD_FILE is set but could not be read: {password_file}"
            ) from error

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


# -----------------------------------------------------------------------------
# Search transport (Phase 3.4K, controlled rollout -- see
# scripts/search_transport/). Deliberately independent of TOR_ENABLED above:
# TOR_ENABLED governs whether the Tor capability/infra is considered live at
# all (dark launch, monitoring, container health) and this repository has an
# actively-enforced invariant (tests/test_tor_production_dark_launch.py::
# test_tor_enabled_setters_are_a_closed_allowlist and
# ::test_no_collection_script_sets_tor_enabled_true) that no collection code
# path may read or depend on it. SEARCH_TRANSPORT is this phase's own,
# self-sufficient rollout flag for whether LinkedIn *traffic* is routed
# through proxy transport. Default is "direct", which preserves the
# previous no-proxy network route and the same connect/read timeout
# defaults collectors always used. Phase 3.4K still adds classification,
# observability, bounded-retry, auth-safety, and process deadline
# control-flow on top of that default route.
# -----------------------------------------------------------------------------
class InvalidSearchTransportConfigError(ValueError):
    """Raised when SEARCH_TRANSPORT is explicitly set to a value that is
    neither "direct" nor "proxy". Deliberately NOT the same code path as
    "genuinely unset" (see get_search_transport_mode()) -- a typo such as
    SEARCH_TRANSPORT=proxxy must fail closed with a clear configuration
    error, never silently fall back to "direct" and produce unexpected
    direct-network traffic instead of the proxy transport an operator
    actually intended."""


def get_search_transport_mode():
    """"direct" when SEARCH_TRANSPORT is genuinely unset (or set to an
    empty string) -- this is the documented, pre-existing default value.
    Any other explicitly-configured value that isn't "direct" or
    "proxy" (case-insensitive) raises InvalidSearchTransportConfigError
    rather than being coerced to "direct" -- see
    InvalidSearchTransportConfigError for why."""
    raw_value = os.environ.get("SEARCH_TRANSPORT")

    if raw_value is None or raw_value.strip() == "":
        return "direct"

    value = raw_value.strip().lower()

    if value not in ("direct", "proxy"):
        raise InvalidSearchTransportConfigError(
            f"Invalid SEARCH_TRANSPORT={raw_value!r}: must be 'direct' or "
            "'proxy' (or unset, which defaults to 'direct')."
        )

    return value


def get_search_transport_direct_connect_timeout_ms():
    """Matches the navigation timeout LinkedInBrowserProvider hardcoded
    before this phase (120000ms) -- direct mode's default connect timeout
    value must remain exactly that."""
    return _get_int_env_value("SEARCH_TRANSPORT_DIRECT_CONNECT_TIMEOUT_MS", 120000)


def get_search_transport_direct_read_timeout_ms():
    """Matches the default Playwright action timeout LinkedInBrowserProvider
    hardcoded before this phase (30000ms)."""
    return _get_int_env_value("SEARCH_TRANSPORT_DIRECT_READ_TIMEOUT_MS", 30000)


def get_search_transport_proxy_connect_timeout_ms():
    """Wider than direct mode by default: Tor circuit build/handshake time
    is higher-latency and higher-variance than a direct connection. A slow
    Tor connect must not be misclassified as a LinkedIn block."""
    return _get_int_env_value("SEARCH_TRANSPORT_PROXY_CONNECT_TIMEOUT_MS", 180000)


def get_search_transport_proxy_read_timeout_ms():
    return _get_int_env_value("SEARCH_TRANSPORT_PROXY_READ_TIMEOUT_MS", 45000)


def get_search_transport_proxy_probe_timeout_ms():
    """How long the proxy transport waits for a TCP connect to the SOCKS
    port before declaring the proxy unavailable (see
    scripts/search_transport/transport.py). Deliberately short and
    separate from the navigation timeouts above -- this only proves the
    SOCKS port accepts TCP connections, not that Tor is bootstrapped."""
    return _get_int_env_value("SEARCH_TRANSPORT_PROXY_PROBE_TIMEOUT_MS", 3000)


def get_linkedin_plan_collect_query_timeout_seconds():
    """Per-query deadline (Phase 3.4K Stabilization, Section 7) around the
    scripts.collector_postgres subprocess scripts/linkedin_plan_collect.py
    launches per query, enforced via scripts.run_with_deadline. Without
    this, ONE wedged collector subprocess (a Playwright hang, a blocked
    network call, etc.) could silently consume the entire OUTER step
    budget (see scripts/run_collection_cycle_safe.sh's
    STEP_TIMEOUT_SECONDS, which wraps the whole
    process_search_demand_queue run) and abort every OTHER, healthy query
    in the same batch along with it. Generous relative to this phase's own
    transport timeouts (proxy connect timeout alone can be up to 180s) --
    a single query can involve many pages and per-job detail-panel
    clicks."""
    return max(60, _get_int_env_value("LINKEDIN_PLAN_COLLECT_QUERY_TIMEOUT_SECONDS", 900))


def get_linkedin_plan_collect_query_kill_after_seconds():
    """SIGKILL grace period for the deadline above -- passed straight
    through as scripts.run_with_deadline's own --kill-after."""
    return max(1, _get_int_env_value("LINKEDIN_PLAN_COLLECT_QUERY_KILL_AFTER_SECONDS", 15))


def get_search_demand_max_attempts():
    """Per-task retry budget for job_search_demand_queue rows (see
    scripts/process_search_demand_queue.py). A row's fail_count reaching
    this value moves it to the terminal 'failed' status instead of being
    requeued to 'pending' again -- the fix for the previously-unbounded
    retry loop (Phase 3.4K, Section 8)."""
    return max(1, _get_int_env_value("SEARCH_DEMAND_MAX_ATTEMPTS", 3))
