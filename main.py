# Configure logging
import logging
import os
import secrets
import time

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from quart import Quart, Response, request

from classes import AsyncDb, BackendManager, IPTracker, ModSecurityEngine, RateLimiter
from decorators import apython_lb, internal_only, modsec_waf, rate_limit
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


@app.after_serving
async def shutdown():
    await backend_manager.close()


def _endpoint_label() -> str:
    return request.url_rule.rule if request.url_rule else "unmatched"


@app.before_request
async def _start_request_timer():
    request._metrics_start = time.monotonic()
    HTTP_REQUESTS_IN_PROGRESS.labels(method=request.method, endpoint=_endpoint_label()).inc()


@app.after_request
async def _record_request_metrics(response):
    endpoint = _endpoint_label()
    HTTP_REQUESTS_IN_PROGRESS.labels(method=request.method, endpoint=endpoint).dec()
    duration = time.monotonic() - getattr(request, "_metrics_start", time.monotonic())
    HTTP_REQUEST_DURATION_SECONDS.labels(method=request.method, endpoint=endpoint).observe(duration)
    HTTP_REQUESTS_TOTAL.labels(
        method=request.method, endpoint=endpoint, status=response.status_code
    ).inc()
    return response


@app.route("/", defaults={"path": ""}, endpoint="index_default")
@app.route("/<path:path>", methods=["GET", "POST"], endpoint="index_default")
@rate_limit
@modsec_waf
@apython_lb(backend_name="my_backend", route_policy="round_robin")
def index(path):
    pass


# Strip a /v1 prefix before forwarding
@app.route("/v1/", defaults={"path": ""})
@app.route("/v1/<path:path>", methods=["GET", "POST"])
@rate_limit
@modsec_waf
@apython_lb(
    backend_name="dns_backend",
    route_policy="sticky_ip",
    url_match=r"^v1/(.+)$",
    url_rewrite=r"\1",
)
def v1_proxy(path):
    pass


@app.route("/health")
async def health_check():
    return {"status": "healthy"}, 200


# Prometheus metrics leak operational detail (backend pool names, health
# counts), so scraping is restricted to internal networks like /v1/manage.
@app.route("/metrics")
@internal_only
async def metrics():
    return Response(generate_latest(), content_type=CONTENT_TYPE_LATEST)


if __name__ == "__main__":
    app.run()
