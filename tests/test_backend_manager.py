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
