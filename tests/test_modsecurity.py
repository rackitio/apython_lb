import pytest
from quart import Quart

from classes.modsecurity import ModSecurityEngine
from decorators.modsec_waf import modsec_waf


class FakeModSecurityEngine:
    """Stand-in for ModSecurityEngine used in decorator tests."""

    def __init__(self, allow: bool = True, status_code: int = 403):
        self.enabled = True
        self._allow = allow
        self._status_code = status_code
        self.inspected: list[dict] = []

    async def inspect_request(self, client_ip, method, uri, headers, body):
        self.inspected.append({"client_ip": client_ip, "method": method, "uri": uri})
        if self._allow:
            return True, 200
        return False, self._status_code


def make_app(modsec=None):
    app = Quart(__name__)

    if modsec is not None:
        app.modsec = modsec

    @app.route("/", defaults={"path": ""})
    @app.route("/<path:path>")
    @modsec_waf
    async def handler(path):
        return "ok", 200

    return app


# ── ModSecurityEngine unit tests ─────────────────────────────────────── #


def test_engine_disabled_by_default():
    engine = ModSecurityEngine(enabled=False)
    assert not engine.enabled


@pytest.mark.asyncio
async def test_engine_disabled_allows_all():
    engine = ModSecurityEngine(enabled=False)
    allowed, status = await engine.inspect_request("1.2.3.4", "GET", "http://example.com/", {}, b"")
    assert allowed
    assert status == 200


# ── modsec_waf decorator tests ────────────────────────────────────────── #


@pytest.mark.asyncio
async def test_modsec_waf_allows_clean_request():
    modsec = FakeModSecurityEngine(allow=True)
    app = make_app(modsec=modsec)

    resp = await app.test_client().get("/some/path")

    assert resp.status_code == 200
    assert len(modsec.inspected) == 1
    assert modsec.inspected[0]["method"] == "GET"


@pytest.mark.asyncio
async def test_modsec_waf_blocks_malicious_request():
    modsec = FakeModSecurityEngine(allow=False, status_code=403)
    app = make_app(modsec=modsec)

    resp = await app.test_client().get("/evil")

    assert resp.status_code == 403
    assert len(modsec.inspected) == 1


@pytest.mark.asyncio
async def test_modsec_waf_blocked_status_code_propagated():
    modsec = FakeModSecurityEngine(allow=False, status_code=406)
    app = make_app(modsec=modsec)

    resp = await app.test_client().get("/")

    assert resp.status_code == 406


@pytest.mark.asyncio
async def test_modsec_waf_passthrough_without_app_modsec():
    """Decorator is a no-op when app.modsec is not set."""
    app = make_app(modsec=None)

    resp = await app.test_client().get("/")

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_modsec_waf_inspects_request_details():
    modsec = FakeModSecurityEngine(allow=True)
    app = make_app(modsec=modsec)

    await app.test_client().get("/api/resource")

    call = modsec.inspected[0]
    assert call["method"] == "GET"
    assert "/api/resource" in call["uri"]


# ── Security-hardening regression tests ──────────────────────────────── #


@pytest.mark.asyncio
async def test_modsec_waf_inspects_request_uri_not_absolute_url():
    """Regression: the decorator passed request.url (scheme + host + path),
    skewing REQUEST_URI-based rules such as length limits."""
    modsec = FakeModSecurityEngine(allow=True)
    app = make_app(modsec=modsec)

    await app.test_client().get("/api/resource?q=1")
    await app.test_client().get("/plain")

    assert modsec.inspected[0]["uri"] == "/api/resource?q=1"
    assert modsec.inspected[1]["uri"] == "/plain"


def test_engine_fail_open_disables_waf_when_lib_missing(monkeypatch):
    import classes.modsecurity as modsecurity_module

    monkeypatch.setattr(modsecurity_module, "_load_lib", lambda: None)
    engine = ModSecurityEngine(enabled=True, fail_closed=False)
    assert not engine.enabled


def test_engine_fail_closed_raises_when_lib_missing(monkeypatch):
    """Regression: a requested-but-unloadable WAF used to disable itself with
    only a DEBUG log; with MODSECURITY_FAIL_CLOSED=true it must refuse to start."""
    import classes.modsecurity as modsecurity_module

    monkeypatch.setattr(modsecurity_module, "_load_lib", lambda: None)
    with pytest.raises(RuntimeError):
        ModSecurityEngine(enabled=True, fail_closed=True)
