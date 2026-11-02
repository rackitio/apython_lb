import importlib

import pytest
from quart import Quart

from classes.ip_tracker import IPTracker
from classes.route_manager import RouteEntry
from classes.sticky_ip import StickyIP
from classes.sticky_session import StickyRoundRobin
from decorators.apython_lb import apython_lb, dynamic_route

# "import decorators.apython_lb" would resolve to the function re-exported by
# the package __init__, not the submodule — load the module explicitly.
lb_module = importlib.import_module("decorators.apython_lb")

BACKEND_A = "https://10.0.0.1"
BACKEND_B = "https://10.0.0.2"


class FakeUpstreamResponse:
    status_code = 200
    content = b"ok"
    headers = {}

    def raise_for_status(self):
        pass


class FakeAsyncClient:
    """Stand-in for httpx.AsyncClient that records forwarded URLs."""

    requests: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, **kwargs):
        FakeAsyncClient.requests.append(url)
        return FakeUpstreamResponse()


class FakeBackendManager:
    def __init__(self):
        self._rr = StickyRoundRobin([BACKEND_A, BACKEND_B])
        self._sticky_ip = StickyIP()
        self.dns_resolver = None  # backends are raw IPs, never resolved

    def get_healthy_backends(self, name):
        return self._rr

    def get_sticky_ip(self):
        return self._sticky_ip


def make_app(route_policy: str) -> Quart:
    app = Quart(__name__)
    app.secret_key = "test-secret"
    app.backend_manager = FakeBackendManager()
    app.ip_tracker = IPTracker(threshold=100, window=60, duration=60)

    @app.route("/", defaults={"path": ""}, methods=["GET", "POST"])
    @app.route("/<path:path>", methods=["GET", "POST"])
    @apython_lb(backend_name="pool", route_policy=route_policy)
    def proxy(path):
        pass

    return app


@pytest.fixture(autouse=True)
def fake_httpx(monkeypatch):
    FakeAsyncClient.requests = []
    monkeypatch.setattr(lb_module.httpx, "AsyncClient", FakeAsyncClient)


@pytest.mark.asyncio
async def test_round_robin_alternates_backends():
    app = make_app("round_robin")
    client = app.test_client()

    await client.get("/")
    await client.get("/")

    hosts = [url.rsplit("/", 1)[0] for url in FakeAsyncClient.requests]
    assert hosts == [BACKEND_A, BACKEND_B]


@pytest.mark.asyncio
async def test_sticky_ip_pins_client_to_one_backend():
    app = make_app("sticky_ip")
    client = app.test_client()

    await client.get("/")
    await client.get("/")
    await client.get("/")

    hosts = {url.rsplit("/", 1)[0] for url in FakeAsyncClient.requests}
    assert len(hosts) == 1

    pins = await app.backend_manager.get_sticky_ip().get_all()
    assert list(pins.values()) == [hosts.pop()]


@pytest.mark.asyncio
async def test_sticky_session_pins_session_to_one_backend():
    app = make_app("sticky_session")
    client = app.test_client()  # cookies persist across requests

    await client.get("/")
    await client.get("/")

    hosts = {url.rsplit("/", 1)[0] for url in FakeAsyncClient.requests}
    assert len(hosts) == 1

    session_map = await app.backend_manager._rr.get_session_map()
    assert len(session_map) == 1


# ── Header hygiene and retry-safety regression tests ──────────────────── #


class RecordingClient(FakeAsyncClient):
    """Records forwarded headers and returns hop-by-hop response headers."""

    sent_headers: list[dict] = []

    async def request(self, method, url, **kwargs):
        RecordingClient.sent_headers.append(kwargs.get("headers", {}))
        FakeAsyncClient.requests.append(url)
        resp = FakeUpstreamResponse()
        resp.headers = {
            "Content-Encoding": "gzip",
            "Transfer-Encoding": "chunked",
            "Connection": "keep-alive",
            "Content-Length": "999",
            "X-Upstream": "yes",
        }
        return resp


class RaisingClient(FakeAsyncClient):
    """Raises a given httpx exception for every request."""

    exc_factory = None

    async def request(self, method, url, **kwargs):
        FakeAsyncClient.requests.append(url)
        raise RaisingClient.exc_factory()


@pytest.mark.asyncio
async def test_hop_by_hop_headers_stripped_both_directions(monkeypatch):
    """Regression: hop-by-hop and stale Content-Length/Content-Encoding
    headers were forwarded verbatim in both directions."""
    RecordingClient.sent_headers = []
    monkeypatch.setattr(lb_module.httpx, "AsyncClient", RecordingClient)
    app = make_app("round_robin")

    resp = await app.test_client().get(
        "/", headers={"Connection": "keep-alive", "TE": "trailers", "X-Keep": "yes"}
    )

    sent = {k.lower(): v for k, v in RecordingClient.sent_headers[0].items()}
    assert "connection" not in sent
    assert "te" not in sent
    assert sent["x-keep"] == "yes"

    assert resp.status_code == 200
    assert "Content-Encoding" not in resp.headers
    assert "Transfer-Encoding" not in resp.headers
    assert resp.headers["X-Upstream"] == "yes"


@pytest.mark.asyncio
async def test_post_not_retried_after_read_timeout(monkeypatch):
    """Regression: a POST that timed out mid-request was replayed against
    another backend, double-submitting non-idempotent operations."""
    import httpx as real_httpx

    RaisingClient.exc_factory = lambda: real_httpx.ReadTimeout("upstream stalled")
    monkeypatch.setattr(lb_module.httpx, "AsyncClient", RaisingClient)
    app = make_app("round_robin")

    resp = await app.test_client().post("/submit", data=b"payload")

    assert resp.status_code == 504
    assert len(FakeAsyncClient.requests) == 1  # exactly one attempt, no replay


@pytest.mark.asyncio
async def test_post_retried_on_connect_error(monkeypatch):
    """A connect-phase failure means the backend never saw the request, so
    retrying a POST is safe."""
    import httpx as real_httpx

    RaisingClient.exc_factory = lambda: real_httpx.ConnectError("refused")
    monkeypatch.setattr(lb_module.httpx, "AsyncClient", RaisingClient)
    app = make_app("round_robin")

    resp = await app.test_client().post("/submit", data=b"payload")

    # Both pool backends are attempted and evicted, then no healthy backends remain
    assert len(FakeAsyncClient.requests) == 2
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_get_retried_after_read_timeout(monkeypatch):
    """Idempotent methods keep the old retry behavior on any failure."""
    import httpx as real_httpx

    RaisingClient.exc_factory = lambda: real_httpx.ReadTimeout("upstream stalled")
    monkeypatch.setattr(lb_module.httpx, "AsyncClient", RaisingClient)
    app = make_app("round_robin")

    resp = await app.test_client().get("/thing")

    assert len(FakeAsyncClient.requests) == 2
    assert resp.status_code == 503


# ── dynamic_route tests ─────────────────────────────────────────────────── #
# dynamic_route resolves backend_name/route_policy/url_match/url_rewrite from
# current_app.route_manager.match() at request time instead of from decorator
# kwargs, then calls the exact same _forward() apython_lb() uses above -- so
# these only need to cover the resolution/404/405 behavior that's new; retry,
# header-hygiene, and policy behavior are already covered by the apython_lb()
# tests above, since both decorators share that code.


class FakeRouteManager:
    """Stand-in for classes.route_manager.RouteManager — same match()
    contract (longest-prefix match, (entry, method_allowed) tuple) without
    reading from an actual TOML file."""

    def __init__(self, entries: list[RouteEntry]):
        self._entries = sorted(entries, key=lambda e: len(e.path), reverse=True)

    def match(self, request_path: str, method: str):
        method = method.upper()
        for entry in self._entries:
            if request_path.startswith(entry.path) or request_path == entry.path.rstrip("/"):
                return entry, method in entry.methods
        return None, False


def make_dynamic_app(entries: list[RouteEntry]) -> Quart:
    app = Quart(__name__)
    app.secret_key = "test-secret"
    app.backend_manager = FakeBackendManager()
    app.ip_tracker = IPTracker(threshold=100, window=60, duration=60)
    app.route_manager = FakeRouteManager(entries)

    # Both rules register the same broad method set -- see the matching
    # comment in main.py: "/" must not be narrower than "/<path:path>", or
    # Werkzeug's own 405 would preempt dynamic_route's per-entry methods
    # check for anything configured at path "/".
    proxy_methods = ["GET", "POST", "PUT", "PATCH", "DELETE"]

    @app.route("/", defaults={"path": ""}, methods=proxy_methods)
    @app.route("/<path:path>", methods=proxy_methods)
    @dynamic_route
    def proxy(path):
        pass

    return app


@pytest.mark.asyncio
async def test_dynamic_route_forwards_to_matched_backend():
    app = make_dynamic_app([RouteEntry(path="/", backend_name="pool", route_policy="round_robin")])

    resp = await app.test_client().get("/anything")

    assert resp.status_code == 200
    assert len(FakeAsyncClient.requests) == 1


@pytest.mark.asyncio
async def test_dynamic_route_longest_prefix_wins_and_rewrite_applies():
    entries = [
        RouteEntry(path="/", backend_name="root_pool", route_policy="round_robin"),
        RouteEntry(
            path="/v1/",
            backend_name="v1_pool",
            route_policy="round_robin",
            url_match=r"^v1/(.+)$",
            url_rewrite=r"\1",
        ),
    ]
    app = make_dynamic_app(entries)

    await app.test_client().get("/v1/foo")

    # The more specific "/v1/" entry (not the "/" catch-all) must have been
    # selected, and its url_rewrite must have stripped the "v1/" prefix.
    assert FakeAsyncClient.requests[-1].endswith("/foo")


@pytest.mark.asyncio
async def test_dynamic_route_404_when_no_path_matches():
    app = make_dynamic_app(
        [RouteEntry(path="/v1/", backend_name="pool", route_policy="round_robin")]
    )

    resp = await app.test_client().get("/unmatched")

    assert resp.status_code == 404
    assert not FakeAsyncClient.requests  # request never reached a backend


@pytest.mark.asyncio
async def test_dynamic_route_405_when_method_not_allowed():
    entries = [
        RouteEntry(
            path="/",
            backend_name="pool",
            route_policy="round_robin",
            methods=frozenset({"GET"}),
        )
    ]
    app = make_dynamic_app(entries)

    resp = await app.test_client().post("/")

    assert resp.status_code == 405
    assert resp.headers["Allow"] == "GET"
    assert not FakeAsyncClient.requests
