import base64

from decorators.ip_restriction import basic_auth_ok, is_internal_ip


def _basic(creds: str) -> str:
    return "Basic " + base64.b64encode(creds.encode()).decode()


def test_is_internal_ip():
    assert is_internal_ip("10.1.2.3")
    assert is_internal_ip("172.16.0.45")
    assert is_internal_ip("192.168.1.1")
    assert is_internal_ip("127.0.0.1")
    assert not is_internal_ip("8.8.8.8")
    assert not is_internal_ip("172.32.0.1")  # just outside 172.16/12
    assert not is_internal_ip("not-an-ip")
    assert not is_internal_ip("")


def test_basic_auth_not_required_when_unset(monkeypatch):
    monkeypatch.delenv("MANAGE_BASIC_AUTH", raising=False)
    assert basic_auth_ok(None)
    assert basic_auth_ok("Basic anything")


def test_basic_auth_enforced_when_set(monkeypatch):
    monkeypatch.setenv("MANAGE_BASIC_AUTH", "admin:s3cret")

    assert basic_auth_ok(_basic("admin:s3cret"))

    assert not basic_auth_ok(None)
    assert not basic_auth_ok("")
    assert not basic_auth_ok(_basic("admin:wrong"))
    assert not basic_auth_ok(_basic("other:s3cret"))
    assert not basic_auth_ok("Basic !!!not-base64!!!")
    assert not basic_auth_ok("Bearer sometoken")
