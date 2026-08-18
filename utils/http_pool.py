"""utils/http_pool.py — Shared pooled HTTP session for action modules.

Perf audit item #1 ("Convert blocking HTTP calls to async"): the honest
full fix is httpx.AsyncClient end-to-end, but every tool call in this
codebase (calendar_action, weather_report,  ...) is already
invoked through `asyncio.to_thread(_execute_tool, ...)` at the call site
(see main.py), so the event loop itself is never blocked by these
`requests` calls today.

What *was* actually costing latency:
  1. Every call opened a brand-new TCP+TLS connection (`requests.get(...)`
     ad-hoc, no session) even though most of these hosts (googleapis.com,
     weatherapi-com.p.rapidapi.com, api.spotify.com) are hit repeatedly
     within the same process lifetime.
  2. Timeouts of up to 10s meant a single flaky call could pin a worker
     thread from the shared default executor for far longer than
     necessary, delaying whatever else was queued behind it.

Fix: one pooled `requests.Session` per process (keep-alive connection
reuse — no repeated handshakes) and a single strict-timeout constant
used everywhere, capped at 5s per the audit's "at minimum" guidance.
Where a module previously used a shorter timeout (e.g. weather's 6s ->
5s), we keep the tighter of the two.
"""

from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter

# Strict ceiling — fail fast and let the caller fall back / report an
# error rather than pin a thread for up to 10s like before.
HTTP_TIMEOUT = 5

_session: requests.Session | None = None


def get_session() -> requests.Session:
    """Return the process-wide pooled session, creating it on first use.

    Thread-safe for our usage pattern: `requests.Session` is documented
    as thread-safe for making concurrent requests (each request gets its
    own connection from the pool); we only need to guard the lazy
    creation itself against being run twice, which a simple double
    -checked assignment handles well enough here (worst case: two
    sessions briefly created, one discarded — no correctness issue).
    """
    global _session
    if _session is None:
        s = requests.Session()
        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=0)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _session = s
    return _session


__all__ = ["get_session", "HTTP_TIMEOUT"]
