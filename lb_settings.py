"""
Shared upstream-client settings.

Lives at the repo root (not under classes/ or decorators/) so both packages
can import it without creating a circular import. All values come from
environment variables read once at import time.
"""

import os

# ── Retry / circuit-breaker tunables ──────────────────────────────────── #
LB_MAX_ATTEMPTS = int(os.environ.get("LB_MAX_ATTEMPTS", 3))  # total tries per request

# ── Upstream httpx client tunables ────────────────────────────────────── #
LB_UPSTREAM_HTTP2 = os.environ.get("LB_UPSTREAM_HTTP2", "true").lower() == "true"
# Verify backend TLS certificates by default. Set LB_UPSTREAM_VERIFY_TLS=false
# only for backends with self-signed certs you cannot fix.
LB_UPSTREAM_VERIFY_TLS = os.environ.get("LB_UPSTREAM_VERIFY_TLS", "true").lower() == "true"
LB_UPSTREAM_TIMEOUT_SECONDS = float(os.environ.get("LB_UPSTREAM_TIMEOUT_SECONDS", 30.0))
