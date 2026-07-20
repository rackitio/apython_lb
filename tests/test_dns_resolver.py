import socket

import pytest

import classes.dns_resolver as dns_resolver_module
from classes.dns_resolver import AsyncDNSResolver


def test_resolve_nameserver_hosts_resolves_mixed_values():
    local_ip = socket.gethostbyname("localhost")
    resolved = AsyncDNSResolver.resolve_nameserver_hosts("localhost,1.1.1.1,invalid-host,,")

    assert local_ip in resolved
    assert "1.1.1.1" in resolved
    assert all(isinstance(item, str) for item in resolved)


def test_set_nameservers_resets_resolver():
    resolver = AsyncDNSResolver(nameservers=["8.8.8.8"])
    resolver._resolver = object()
    resolver.set_nameservers(["1.1.1.1"])

    assert resolver.nameservers == ["1.1.1.1"]
    assert resolver._resolver is None


@pytest.mark.asyncio
async def test_resolve_and_resolve_with_ttl_with_mocked_aiodns(monkeypatch):
    class FakeRecord:
        def __init__(self, host, ttl):
            self.host = host
            self.ttl = ttl

    async def fake_query(self, hostname, qtype):
        return [FakeRecord("127.0.0.1", 300)]

    monkeypatch.setattr(dns_resolver_module.aiodns.DNSResolver, "query", fake_query)

    resolver = AsyncDNSResolver()
    resolved = await resolver.resolve("localhost")
    assert resolved == ["127.0.0.1"]

    resolved_ttl = await resolver.resolve_with_ttl("localhost")
    assert resolved_ttl == [("127.0.0.1", 300)]
