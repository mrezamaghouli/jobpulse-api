import logging
import os
import time
import uuid
from collections import defaultdict, deque
from urllib.parse import parse_qs

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


logger = logging.getLogger("jobpulse.api_guard")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class ApiGuardMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)

        self.max_query_length = _env_int("API_MAX_QUERY_LENGTH", 120)
        self.max_limit = _env_int("API_MAX_LIMIT", 50)
        self.max_page = _env_int("API_MAX_PAGE", 100)
        self.max_url_length = _env_int("API_MAX_URL_LENGTH", 2048)

        self.rate_limit_enabled = _env_bool("API_ENABLE_RATE_LIMIT", True)
        self.general_rate_limit = _env_int("API_RATE_LIMIT_PER_MINUTE", 120)
        self.search_rate_limit = _env_int("API_SEARCH_RATE_LIMIT_PER_MINUTE", 40)

        self.allowed_methods = {
            item.strip().upper()
            for item in os.getenv("API_ALLOWED_METHODS", "GET,POST,OPTIONS").split(",")
            if item.strip()
        }

        self._hits = defaultdict(deque)

    def _client_ip(self, request):
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()

        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip.strip()

        if request.client:
            return request.client.host

        return "unknown"

    def _json_error(self, status_code: int, message: str, request_id: str):
        return JSONResponse(
            status_code=status_code,
            content={
                "error": message,
                "request_id": request_id,
            },
            headers={
                "X-Request-ID": request_id,
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
            },
        )

    def _check_rate_limit(self, key: str, limit: int) -> bool:
        now = time.time()
        window_start = now - 60

        bucket = self._hits[key]

        while bucket and bucket[0] < window_start:
            bucket.popleft()

        if len(bucket) >= limit:
            return False

        bucket.append(now)
        return True

    def _validate_jobs_query(self, request, request_id: str):
        query_string = request.url.query or ""
        params = parse_qs(query_string)

        for key in ("query", "q", "keywords"):
            value = (params.get(key) or [""])[0].strip()
            if len(value) > self.max_query_length:
                return self._json_error(
                    400,
                    f"{key} is too long. Maximum allowed length is {self.max_query_length}.",
                    request_id,
                )

        limit_raw = (params.get("limit") or [""])[0]
        page_raw = (params.get("page") or [""])[0]

        if limit_raw:
            try:
                limit = int(limit_raw)
                if limit < 1 or limit > self.max_limit:
                    return self._json_error(
                        400,
                        f"limit must be between 1 and {self.max_limit}.",
                        request_id,
                    )
            except ValueError:
                return self._json_error(400, "limit must be a number.", request_id)

        if page_raw:
            try:
                page = int(page_raw)
                if page < 1 or page > self.max_page:
                    return self._json_error(
                        400,
                        f"page must be between 1 and {self.max_page}.",
                        request_id,
                    )
            except ValueError:
                return self._json_error(400, "page must be a number.", request_id)

        return None

    async def dispatch(self, request, call_next):
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        path = request.url.path
        client_ip = self._client_ip(request)

        if request.method.upper() not in self.allowed_methods:
            return self._json_error(405, "Method not allowed.", request_id)

        if len(str(request.url)) > self.max_url_length:
            return self._json_error(
                414,
                f"URL is too long. Maximum allowed length is {self.max_url_length}.",
                request_id,
            )

        if self.rate_limit_enabled:
            is_search = path.startswith("/api/jobs") or path == "/jobs"
            limit = self.search_rate_limit if is_search else self.general_rate_limit
            rate_key = f"{client_ip}:{'search' if is_search else 'general'}"

            if not self._check_rate_limit(rate_key, limit):
                return self._json_error(
                    429,
                    "Too many requests. Please slow down.",
                    request_id,
                )

        if path.startswith("/api/jobs") or path == "/jobs":
            validation_error = self._validate_jobs_query(request, request_id)
            if validation_error is not None:
                return validation_error

        start = time.time()

        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "Unhandled API error request_id=%s path=%s client_ip=%s",
                request_id,
                path,
                client_ip,
            )
            return self._json_error(500, "Internal server error.", request_id)

        duration_ms = round((time.time() - start) * 1000, 2)

        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"

        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"

        logger.info(
            "request_done request_id=%s method=%s path=%s status=%s duration_ms=%s client_ip=%s",
            request_id,
            request.method,
            path,
            response.status_code,
            duration_ms,
            client_ip,
        )

        return response
