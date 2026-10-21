from pathlib import Path

import pytest
from quart import Quart

from classes.modsecurity import ModSecurityEngine
from decorators.modsec_waf import modsec_waf

REAL_RULES_FILE = str(Path(__file__).resolve().parent.parent / "modsecurity.conf")


def _multipart_body(boundary, fields=(), files=()):
    """Builds a raw multipart/form-data body, mirroring what a browser (or
    PHP2Shell's exploit tooling) sends -- used to drive the *real*
    ModSecurityEngine against the shipped modsecurity.conf, not a mock."""
    parts = []
    for name, value in fields:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
        )
    for name, filename, content in files:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
            f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n{content}\r\n'
        )
    parts.append(f"--{boundary}--\r\n")
    return "".join(parts).encode()


def _real_engine():
    """A ModSecurityEngine backed by the real libmodsecurity and the actual
    shipped modsecurity.conf (not FakeModSecurityEngine) -- skips, rather
    than fails, when libmodsecurity itself isn't installed (e.g. running
    pytest outside the Docker image), matching how the engine already
    degrades at runtime (see test_engine_fail_open_disables_waf_when_lib_missing)."""
    engine = ModSecurityEngine(rules_file=REAL_RULES_FILE, enabled=True, fail_closed=False)
    if not engine.enabled:
        pytest.skip("libmodsecurity not available in this environment")
    return engine


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


@pytest.mark.asyncio
async def test_engine_blocks_null_byte_in_uri_without_dispatching():
    """Regression: msc_process_uri() takes the URI as a null-terminated
    `const char *` with no length argument, so a \\x00 in the URI silently
    truncates before ModSecurity ever sees it and rule 910001 never fires —
    the untruncated string then reaches httpx downstream and blows up with
    an unhandled 500 instead of a clean 400. Enforce it in Python instead,
    before any C dispatch (self._lib is intentionally never touched here)."""
    engine = ModSecurityEngine(enabled=False)
    engine.enabled = True  # exercise the guard without a real libmodsecurity
    allowed, status = await engine.inspect_request("1.2.3.4", "GET", "/test\x00.php", {}, b"")
    assert not allowed
    assert status == 400


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


# ── PHP2Shell (CVE-2026-109618 / CVE-2026-109619) mitigation rules ─────── #
# These drive the *real* libmodsecurity engine against the shipped
# modsecurity.conf (not FakeModSecurityEngine) -- the thing actually under
# test is whether the rule text in that file does what it claims, the same
# way a real-upstream SecRequestBodyNoFilesLimit was found (during the
# investigation that added these rules) to not enforce at all despite being
# set. A mock can't catch that kind of gap.


@pytest.mark.asyncio
async def test_real_engine_allows_ordinary_multipart_request():
    engine = _real_engine()
    boundary = "----boundaryOrdinary"
    body = _multipart_body(
        boundary,
        fields=[("caption", "a normal comment")],
        files=[("avatar", "pic.jpg", "x" * 2000)],
    )
    allowed, status = await engine.inspect_request(
        "203.0.113.5",
        "POST",
        "/upload",
        {"Content-Type": f"multipart/form-data; boundary={boundary}"},
        body,
    )
    assert allowed
    assert status == 200


@pytest.mark.asyncio
async def test_real_engine_blocks_php_session_upload_progress_field():
    """session.upload_progress (on by default in PHP) is a confirmed attack
    path for both CVE-2026-109618 and CVE-2026-109619. Legitimate apps don't
    submit this field themselves, so rule 910040 blocks it outright."""
    engine = _real_engine()
    boundary = "----boundaryUploadProgress"
    body = _multipart_body(
        boundary,
        fields=[("PHP_SESSION_UPLOAD_PROGRESS", "1")],
        files=[("file", "x.txt", "hello")],
    )
    allowed, status = await engine.inspect_request(
        "203.0.113.5",
        "POST",
        "/upload.php",
        {"Content-Type": f"multipart/form-data; boundary={boundary}"},
        body,
    )
    assert not allowed
    assert status == 403


@pytest.mark.asyncio
async def test_real_engine_blocks_oversized_form_field():
    """CVE-2026-109618 triggers when PHP hits its own memory_limit mid
    -request. Rule 910042 caps individual non-file field size -- this is
    also the regression check for SecRequestBodyNoFilesLimit silently not
    enforcing (see the comment on it in modsecurity.conf): a field well
    past that documented 131072-byte limit must still be blocked."""
    engine = _real_engine()
    boundary = "----boundaryOversized"
    body = _multipart_body(boundary, fields=[("comment", "A" * 200_000)])
    allowed, status = await engine.inspect_request(
        "203.0.113.5",
        "POST",
        "/upload.php",
        {"Content-Type": f"multipart/form-data; boundary={boundary}"},
        body,
    )
    assert not allowed
    assert status == 413


@pytest.mark.asyncio
async def test_real_engine_allows_field_under_size_threshold():
    """Sanity check for the rule above: ordinary-sized fields aren't caught
    up in it -- a large legitimate file alongside a small field must pass."""
    engine = _real_engine()
    boundary = "----boundaryUnderThreshold"
    body = _multipart_body(
        boundary,
        fields=[("caption", "a normal comment")],
        files=[("photo", "beach.jpg", "B" * 1_000_000)],
    )
    allowed, status = await engine.inspect_request(
        "203.0.113.5",
        "POST",
        "/upload.php",
        {"Content-Type": f"multipart/form-data; boundary={boundary}"},
        body,
    )
    assert allowed
    assert status == 200


@pytest.mark.asyncio
async def test_real_engine_blocks_malformed_multipart_body():
    """A multipart body ModSecurity's own processor can't parse (truncated,
    no closing boundary) is rejected by rule 910041 rather than forwarded
    upstream for the backend's own parser to make sense of on its own."""
    engine = _real_engine()
    boundary = "----boundaryMalformed"
    body = f'--{boundary}\r\nContent-Disposition: form-data; name="x"\r\n\r\nincomplete, no closing boundary'.encode()
    allowed, status = await engine.inspect_request(
        "203.0.113.5",
        "POST",
        "/upload.php",
        {"Content-Type": f"multipart/form-data; boundary={boundary}"},
        body,
    )
    assert not allowed
    assert status == 400
