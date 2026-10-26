# Configure logging
import logging
import os
import secrets
import time

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from quart import Quart, Response, request

from classes import AsyncDb, BackendManager, IPTracker, ModSecurityEngine, RateLimiter, RouteManager
from decorators import dynamic_route, internal_only, modsec_waf, rate_limit
from log_config import configure_logging
from metrics import (
    HTTP_REQUEST_DURATION_SECONDS,
    HTTP_REQUESTS_IN_PROGRESS,
    HTTP_REQUESTS_TOTAL,
)
from routes.v1_manage import v1_manage
from routes.ws_configs import ws_configs

logger = logging.getLogger(__name__)

# Apply logging config before any app objects are created. LOG_LEVEL env var
# controls the level for all app loggers (default INFO).
configure_logging()

app = Quart(__name__)
db = AsyncDb()
backend_manager = BackendManager(db)
app.backend_manager = backend_manager
route_manager = RouteManager()
app.route_manager = route_manager
app.ip_tracker = IPTracker(
    threshold=int(os.environ.get("IP_TRACKER_404_THRESHOLD", 4)),
    window=int(os.environ.get("IP_TRACKER_WINDOW_SECONDS", 120)),  # count 404s within this window
    duration=int(os.environ.get("IP_TRACKER_PENALTY_DURATION", 300)),  # block for this many seconds
)
app.rate_limiter = RateLimiter(
    limit=int(os.environ.get("RATE_LIMIT_REQUESTS", 100)),  # max requests per IP per window
    window=int(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", 60)),
)
app.modsec = ModSecurityEngine()

# blueprints
app.register_blueprint(v1_manage)
app.register_blueprint(ws_configs)

app.config["PROVIDE_AUTOMATIC_OPTIONS"] = True

# Explicit request-body cap. The WAF and proxy buffer bodies in memory, so the
# bound should be deliberate, not an implicit framework default.
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_CONTENT_LENGTH", 16 * 1024 * 1024))

# Secret key for sticky-session cookie signing. Never ship a hardcoded
# default: if unset, generate a random per-process key (sticky sessions then
# won't survive restarts or span replicas — set APP_SECRET_KEY for that).
app.secret_key = os.environ.get("APP_SECRET_KEY")
if not app.secret_key:
    app.secret_key = secrets.token_hex(32)
    logger.warning(
        "APP_SECRET_KEY not set — generated a random per-process secret key. "
        "Sticky sessions will not survive restarts or be shared across replicas."
    )


@app.before_serving
async def startup():
    await db.initialize()
    app.add_background_task(backend_manager.start)

    # Loaded synchronously (and fail loud if routes.toml is missing/invalid)
    # so the route table is populated before the app accepts its first
    # request -- route_manager.start() below only handles reloading after
    # this, not the initial load.
    route_manager.load_now()
    app.add_background_task(route_manager.start)


@app.after_serving
async def shutdown():
    await backend_manager.close()


def _endpoint_label() -> str:
    """
    Used for HTTP_REQUESTS_IN_PROGRESS, which is inc()'d in _start_request_timer
    (before routing resolves which dynamic route matched) and dec()'d in
    _record_request_metrics (after). Both calls must use the same label or the
    gauge drifts out of sync per-label, so this always uses the Werkzeug rule
    -- known before the view runs -- never dynamic_route's more specific
    per-entry label. See _metrics_endpoint_label() for that.
    """
    return request.url_rule.rule if request.url_rule else "unmatched"


def _metrics_endpoint_label() -> str:
    """
    Used by HTTP_REQUEST_DURATION_SECONDS/HTTP_REQUESTS_TOTAL, both recorded
    exactly once per request in _record_request_metrics -- no paired inc/dec
    to keep consistent, so unlike _endpoint_label() above this can safely
    prefer the specific route dynamic_route resolved (decorators/apython_lb.py
    stashes it on request._route_entry_path), so dashboards keep per-route
    labels instead of every dynamic route collapsing into one generic
    "/<path:path>" bucket. Falls back to the Werkzeug rule for static routes
    (/health, /metrics) and unmatched requests, same as before this existed.
    """
    return getattr(request, "_route_entry_path", None) or _endpoint_label()


@app.before_request
async def _start_request_timer():
    request._metrics_start = time.monotonic()
    HTTP_REQUESTS_IN_PROGRESS.labels(method=request.method, endpoint=_endpoint_label()).inc()


@app.after_request
async def _record_request_metrics(response):
    HTTP_REQUESTS_IN_PROGRESS.labels(method=request.method, endpoint=_endpoint_label()).dec()
    endpoint = _metrics_endpoint_label()
    duration = time.monotonic() - getattr(request, "_metrics_start", time.monotonic())
    HTTP_REQUEST_DURATION_SECONDS.labels(method=request.method, endpoint=endpoint).observe(duration)
    HTTP_REQUESTS_TOTAL.labels(
        method=request.method, endpoint=endpoint, status=response.status_code
    ).inc()
    return response


# Every proxied route is configured dynamically via routes.toml
# (ROUTE_CONFIG_FILE, default /app/routes.toml) -- see classes/route_manager.py
# and README.md for the file format. One generic dispatcher here, fanning out
# at request time to however many [[routes]] entries are configured, matched
# by longest path prefix; dynamic_route (decorators/apython_lb.py) does the
# lookup and 404s/405s before any backend is ever selected.
#
# Both rules below register the same broad method set at the Werkzeug level,
# including the "/" rule -- a RouteEntry's own `methods` field is what
# actually narrows things down per path, in dynamic_route. If "/" registered
# a narrower set here (e.g. the GET/HEAD/OPTIONS Werkzeug defaults to when
# methods= is omitted), Werkzeug itself would reject anything else with its
# own 405 before dynamic_route ever got a chance to consult routes.toml --
# which would make a routes.toml entry for path "/" with, say,
# methods = ["POST"] silently impossible to satisfy.
_PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]


@app.route("/", defaults={"path": ""}, methods=_PROXY_METHODS, endpoint="proxy")
@app.route("/<path:path>", methods=_PROXY_METHODS, endpoint="proxy")
@rate_limit
@modsec_waf
@dynamic_route
def proxy(path):
    pass


@app.route("/health")
async def health_check():
    ready, healthy, total = backend_manager.readiness()
    body = {
        "status": "healthy" if ready else "unhealthy",
        "backends_ready": healthy,
        "backends_total": total,
    }
    return body, 200 if ready else 503


# Prometheus metrics leak operational detail (backend pool names, health
# counts), so scraping is restricted to internal networks like /v1/manage.
@app.route("/metrics")
@internal_only
async def metrics():
    return Response(generate_latest(), content_type=CONTENT_TYPE_LATEST)


if __name__ == "__main__":
    app.run()
