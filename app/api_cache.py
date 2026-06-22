import hashlib
import os
import time
from collections import OrderedDict

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response


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


class SimpleApiCacheMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        self.enabled = _env_bool("API_CACHE_ENABLED", True)
        self.ttl_seconds = _env_int("API_CACHE_TTL_SECONDS", 90)
        self.max_items = _env_int("API_CACHE_MAX_ITEMS", 500)
        self.cache = OrderedDict()

    def _is_cacheable(self, request) -> bool:
        if not self.enabled:
            return False

        if request.method.upper() != "GET":
            return False

        path = request.url.path

        # Cache only public read-heavy endpoints.
        if path not in {"/jobs", "/jobs/search", "/api/jobs", "/api/jobs/search"}:
            return False

        # Do not cache debug or admin-like requests if added later.
        query = request.url.query or ""
        if "debug=true" in query or "nocache=true" in query:
            return False

        return True

    def _cache_key(self, request) -> str:
        raw = f"{request.method.upper()}:{request.url.path}?{request.url.query}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _get(self, key: str):
        item = self.cache.get(key)
        if not item:
            return None

        created_at, status_code, headers, body = item

        if time.time() - created_at > self.ttl_seconds:
            self.cache.pop(key, None)
            return None

        self.cache.move_to_end(key)
        return status_code, headers, body

    def _set(self, key: str, status_code: int, headers: dict, body: bytes):
        self.cache[key] = (time.time(), status_code, headers, body)
        self.cache.move_to_end(key)

        while len(self.cache) > self.max_items:
            self.cache.popitem(last=False)

    async def dispatch(self, request, call_next):
        if not self._is_cacheable(request):
            return await call_next(request)

        key = self._cache_key(request)
        cached = self._get(key)

        if cached is not None:
            status_code, headers, body = cached
            headers = dict(headers)
            headers["X-JobPulse-Cache"] = "HIT"
            return Response(
                content=body,
                status_code=status_code,
                headers=headers,
                media_type=headers.get("content-type"),
            )

        response = await call_next(request)

        body = b""
        async for chunk in response.body_iterator:
            body += chunk

        headers = dict(response.headers)
        headers["X-JobPulse-Cache"] = "MISS"

        if response.status_code == 200:
            self._set(key, response.status_code, headers, body)

        return Response(
            content=body,
            status_code=response.status_code,
            headers=headers,
            media_type=headers.get("content-type"),
        )
