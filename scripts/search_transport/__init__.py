"""Search transport abstraction (Phase 3.4K).

See scripts/search_transport/transport.py for the entry point
(get_search_transport()) and module docstrings throughout this package
for the architecture. Collectors should import only from transport.py,
executor.py, and retry_policy.py -- classifier.py and metrics.py are
internal collaborators used by executor.py. This package contains no
Tor circuit-rotation logic and no import of anything under scripts/tor/:
a RequestResult classification (including RATE_LIMIT) is reported to the
caller and never automatically triggers a circuit change (see
executor.py's module docstring).
"""
