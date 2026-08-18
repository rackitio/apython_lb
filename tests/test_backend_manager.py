import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from classes.backend_manager import BackendManager


class DummyDb:
    async def get_all_configs(self):
        return []


@pytest.mark.asyncio
async def test_parse_servers_returns_host_and_server_urls():
    manager = BackendManager(db=DummyDb())

    host_name, servers = await manager._parse_servers(
        ("https", "example.com", "1.2.3.4,app.example.com", "/health")
    )

    assert host_name == "example.com"
    assert "https://1.2.3.4" in servers
    assert "https://app.example.com" in servers


@pytest.mark.asyncio
async def test_resolve_entry_returns_ip_for_hostname(monkeypatch):
    manager = BackendManager(db=DummyDb())
    manager.dns_resolver = SimpleNamespace(resolve=AsyncMock(return_value=["10.0.0.1"]))

    resolved = await manager._resolve_entry("https://example.com", "https")
    assert resolved == ["https://10.0.0.1"]

    raw_ip = await manager._resolve_entry("https://1.2.3.4", "https")
    assert raw_ip == ["https://1.2.3.4"]


@pytest.mark.asyncio
async def test_check_server_returns_true_for_200_and_false_on_error():
    manager = BackendManager(db=DummyDb())
    manager.http_client = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(status_code=200))
    )

    assert await manager._check_server("https://1.2.3.4", "example.com") is True

    manager.http_client.get = AsyncMock(side_effect=httpx.HTTPError("connection failed"))
    assert await manager._check_server("https://1.2.3.4", "example.com") is False


def _config_row(config_id, name, ip, last_selected):
    """Row shaped like AsyncDb.get_all_configs() output."""
    return {
        "id": config_id,
        "name": name,
        "data": ["https", "example.com", ip, "/health"],
        "last_selected": last_selected,
    }


class DuplicateNameDb:
    """DB stub returning rows ordered LastSelected DESC, like the real query."""

    def __init__(self, configs):
        self._configs = configs

    async def get_all_configs(self):
        return self._configs


@pytest.mark.asyncio
async def test_poll_configs_last_config_wins_for_duplicate_names(monkeypatch):
    """Regression: with duplicate names, the most recent config (first row in
    LastSelected DESC order) must control the pool — not the oldest."""
    broadcast = AsyncMock()
    monkeypatch.setattr("classes.backend_manager.broadcast_config_update", broadcast)

    manager = BackendManager(
        db=DuplicateNameDb(
            [
                _config_row(2, "pool", "2.2.2.2", "2026-07-18 12:00:01"),  # newest
                _config_row(1, "pool", "1.1.1.1", "2026-07-18 11:00:00"),  # oldest
            ]
        )
    )

    await manager._poll_configs_once()

    assert manager._configs["pool"] == ("https", "example.com", "2.2.2.2", "/health")
    assert set(manager._backends) == {"pool"}


@pytest.mark.asyncio
async def test_poll_configs_duplicate_names_do_not_rebroadcast_every_pass(monkeypatch):
    """Regression: duplicate names used to flip-flop _configs each pass,
    broadcasting a phantom change on every poll cycle."""
    broadcast = AsyncMock()
    monkeypatch.setattr("classes.backend_manager.broadcast_config_update", broadcast)

    manager = BackendManager(
        db=DuplicateNameDb(
            [
                _config_row(2, "pool", "2.2.2.2", "2026-07-18 12:00:01"),
                _config_row(1, "pool", "1.1.1.1", "2026-07-18 11:00:00"),
            ]
        )
    )

    await manager._poll_configs_once()  # first pass: new config, broadcasts
    await manager._poll_configs_once()  # steady state: must not broadcast again

    assert broadcast.await_count == 1
    assert manager._configs["pool"] == ("https", "example.com", "2.2.2.2", "/health")


# ── readiness() / /health ──────────────────────────────────────────────── #


def test_readiness_ready_on_fresh_install_with_no_configs():
    manager = BackendManager(db=DummyDb())

    ready, healthy_count, total = manager.readiness()

    assert (ready, healthy_count, total) == (True, 0, 0)


def test_readiness_not_ready_until_configured_backends_are_verified_healthy():
    manager = BackendManager(db=DummyDb())
    manager._configs = {"pool": ("https", "example.com", "1.1.1.1", "/health")}

    # No health check has run yet — must not report ready.
    ready, healthy_count, total = manager.readiness()
    assert (ready, healthy_count, total) == (False, 0, 1)

    manager._last_healthy = {"pool": frozenset({"https://1.1.1.1"})}
    ready, healthy_count, total = manager.readiness()
    assert (ready, healthy_count, total) == (True, 1, 1)


def test_readiness_requires_all_configured_backends_by_default():
    manager = BackendManager(db=DummyDb())
    manager._configs = {
        "pool_a": ("https", "a.example.com", "1.1.1.1", "/health"),
        "pool_b": ("https", "b.example.com", "2.2.2.2", "/health"),
    }
    manager._last_healthy = {"pool_a": frozenset({"https://1.1.1.1"})}

    ready, healthy_count, total = manager.readiness()
    assert (ready, healthy_count, total) == (False, 1, 2)

    manager._last_healthy["pool_b"] = frozenset({"https://2.2.2.2"})
    ready, healthy_count, total = manager.readiness()
    assert (ready, healthy_count, total) == (True, 2, 2)


def test_readiness_ready_min_backends_lets_a_subset_satisfy_readiness():
    manager = BackendManager(db=DummyDb(), ready_min_backends=1)
    manager._configs = {
        "pool_a": ("https", "a.example.com", "1.1.1.1", "/health"),
        "pool_b": ("https", "b.example.com", "2.2.2.2", "/health"),
    }
    manager._last_healthy = {"pool_a": frozenset({"https://1.1.1.1"})}

    ready, healthy_count, total = manager.readiness()
    assert (ready, healthy_count, total) == (True, 1, 2)


def test_readiness_ready_min_backends_zero_short_circuits_once_configured():
    manager = BackendManager(db=DummyDb(), ready_min_backends=0)
    manager._configs = {"pool": ("https", "example.com", "1.1.1.1", "/health")}

    ready, healthy_count, total = manager.readiness()
    assert (ready, healthy_count, total) == (True, 0, 1)


def test_readiness_ready_min_backends_clamped_to_configured_total():
    """A tunable larger than the configured backend count must not make
    /health permanently unready."""
    manager = BackendManager(db=DummyDb(), ready_min_backends=10)
    manager._configs = {"pool": ("https", "example.com", "1.1.1.1", "/health")}
    manager._last_healthy = {"pool": frozenset({"https://1.1.1.1"})}

    ready, healthy_count, total = manager.readiness()
    assert (ready, healthy_count, total) == (True, 1, 1)


def test_readiness_ready_backends_only_waits_on_named_backends():
    manager = BackendManager(db=DummyDb(), ready_backends=frozenset({"canary"}))
    manager._configs = {
        "canary": ("https", "canary.example.com", "1.1.1.1", "/health"),
        "slow_pool": ("https", "slow.example.com", "2.2.2.2", "/health"),
    }

    # slow_pool is unhealthy/unchecked but is irrelevant — only "canary" gates readiness.
    ready, healthy_count, total = manager.readiness()
    assert (ready, healthy_count, total) == (False, 0, 1)

    manager._last_healthy = {"canary": frozenset({"https://1.1.1.1"})}
    ready, healthy_count, total = manager.readiness()
    assert (ready, healthy_count, total) == (True, 1, 1)


@pytest.mark.asyncio
async def test_start_populates_configs_before_loops_are_scheduled(monkeypatch):
    """Regression: _health_check_loop's first pass used to race
    _poll_configs_once() and could see an empty config set on startup,
    then sleep a full health_check_interval before ever checking a
    pre-existing backend."""
    manager = BackendManager(
        db=DuplicateNameDb([_config_row(1, "pool", "1.1.1.1", "2026-07-18 11:00:00")])
    )
    monkeypatch.setattr("classes.backend_manager.broadcast_config_update", AsyncMock())

    seen: dict[str, dict] = {}

    async def fake_poll_configs():
        seen.setdefault("poll_configs", dict(manager._configs))
        raise asyncio.CancelledError()

    async def fake_health_check_loop():
        seen.setdefault("health_check_loop", dict(manager._configs))
        raise asyncio.CancelledError()

    manager._poll_configs = fake_poll_configs
    manager._health_check_loop = fake_health_check_loop

    with pytest.raises(asyncio.CancelledError):
        await manager.start()

    assert seen["health_check_loop"] != {}
    assert "pool" in seen["health_check_loop"]
