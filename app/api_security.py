import os
import time
from collections import defaultdict, deque
from typing import Iterable

from fastapi import Request
from fastapi.responses import JSONResponse


PUBLIC_OPEN_PATHS = {
    "/health",
    "/api/health",
    "/docs",
    "/redoc",
    "/openapi.json",
}

PUBLIC_PREFIXES = (
    "/api/admin",
)

PROTECTED_PREFIXES = (
    "/jobs",
)


_rate_buckets: dict[str, deque[float]] = defaultdict(deque)


def _configured_api_keys() -> set[str]:
    raw = os.getenv("JOBPULSE_PUBLIC_API_KEYS", "").strip()
    if not raw:
        return set()

    return {item.strip() for item in raw.split(",") if item.strip()}


def _rate_limit_per_minute() -> int:
    try:
        return max(1, int(os.getenv("JOBPULSE_RATE_LIMIT_PER_MINUTE", "60")))
    except Exception:
        return 60


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()

    if request.client:
        return request.client.host

    return "unknown"


def _api_key_from_request(request: Request) -> str:
    header_key = request.headers.get("x-api-key") or request.headers.get("X-API-Key")
    if header_key:
        return header_key.strip()

    # Optional query param for quick testing only.
    query_key = request.query_params.get("api_key")
    if query_key:
        return query_key.strip()

    return ""


def _is_open_path(path: str) -> bool:
    if path in PUBLIC_OPEN_PATHS:
        return True

    return any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES)


def _is_protected_path(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in PROTECTED_PREFIXES)


def _rate_limited(identity: str, limit: int) -> tuple[bool, int]:
    now = time.time()
    window_start = now - 60

    bucket = _rate_buckets[identity]

    while bucket and bucket[0] < window_start:
        bucket.popleft()

    remaining = max(0, limit - len(bucket))

    if len(bucket) >= limit:
        return True, 0

    bucket.append(now)
    return False, max(0, remaining - 1)


async def public_api_security_middleware(request: Request, call_next):
    path = request.url.path

    if _is_open_path(path) or not _is_protected_path(path):
        return await call_next(request)

    configured_keys = _configured_api_keys()

    # Fail closed when public endpoints are protected but no key is configured.
    if not configured_keys:
        return JSONResponse(
            status_code=503,
            content={
                "error": "api_key_not_configured",
                "message": "Public API key protection is enabled, but no API keys are configured.",
            },
        )

    api_key = _api_key_from_request(request)

    if api_key not in configured_keys:
        return JSONResponse(
            status_code=401,
            content={
                "error": "invalid_api_key",
                "message": "Missing or invalid API key. Send it as X-API-Key.",
            },
        )

    identity = f"key:{api_key[:12]}"
    limit = _rate_limit_per_minute()

    limited, remaining = _rate_limited(identity, limit)

    if limited:
        return JSONResponse(
            status_code=429,
            content={
                "error": "rate_limited",
                "message": f"Rate limit exceeded. Limit is {limit} requests per minute.",
            },
            headers={
                "Retry-After": "60",
                "X-RateLimit-Limit": str(limit),
                "X-RateLimit-Remaining": "0",
            },
        )

    response = await call_next(request)
    response.headers["X-RateLimit-Limit"] = str(limit)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    return response
