"""
Central Prometheus metric definitions.

All metrics live here so they're registered exactly once against the default
registry, regardless of how many modules/classes report against them.
Import the specific metric objects you need rather than redefining them.
"""

from prometheus_client import Counter, Gauge, Histogram

# ── HTTP layer (main.py) ──────────────────────────────────────────────── #

HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Total HTTP requests handled by the app",
    ["method", "endpoint", "status"],
)

HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "endpoint"],
)

HTTP_REQUESTS_IN_PROGRESS = Gauge(
    "http_requests_in_progress",
    "HTTP requests currently being handled",
    ["method", "endpoint"],
)

# ── Load balancer decorator (decorators/apython_lb.py) ───────────────── #

LB_BACKEND_SELECTIONS_TOTAL = Counter(
    "lb_backend_selections_total",
    "Backend selections made by the load balancer",
    ["backend_name", "policy"],
)

LB_RETRIES_TOTAL = Counter(
    "lb_retries_total",
    "Retries against a backend after a connection error or timeout",
    ["backend_name"],
)

LB_BACKEND_ERRORS_TOTAL = Counter(
    "lb_backend_errors_total",
    "Backend request failures by type",
    ["backend_name", "error_type"],
)

LB_NO_HEALTHY_BACKENDS_TOTAL = Counter(
    "lb_no_healthy_backends_total",
    "Requests that found no healthy backend to route to",
    ["backend_name"],
)

# ── Backend health (classes/backend_manager.py) ──────────────────────── #

BACKEND_HEALTHY_SERVERS = Gauge(
    "backend_healthy_servers",
    "Number of currently healthy servers in a backend pool",
    ["backend_name"],
)

# ── Rate limiting (classes/rate_limiter.py) ───────────────────────────── #

RATE_LIMIT_REJECTIONS_TOTAL = Counter(
    "rate_limit_rejections_total",
    "Requests rejected for exceeding the rate limit",
)

# ── IP / 404 tracking (classes/ip_tracker.py) ─────────────────────────── #

IP_TRACKER_404S_TOTAL = Counter(
    "ip_tracker_404s_total",
    "404 responses recorded by the IP tracker",
)

IP_TRACKER_PENALTIES_TOTAL = Counter(
    "ip_tracker_penalties_total",
    "Penalties applied to IPs for crossing the 404 threshold",
)

# ── WAF (classes/modsecurity.py) ──────────────────────────────────────── #

WAF_INSPECTIONS_TOTAL = Counter(
    "waf_inspections_total",
    "Requests inspected by the ModSecurity WAF",
    ["action"],  # "allowed" or "blocked"
)

WAF_ENGINE_ENABLED = Gauge(
    "waf_engine_enabled",
    "1 when the ModSecurity engine is active, 0 when disabled or failed to load",
)
