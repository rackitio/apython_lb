import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from classes.backend_manager import BackendManager, _backends_from_raw, _validate_backend


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


# ── file-defined backends ────────────────────────────────────────────────── #


class FakeConfigFile:
    """Stand-in for ConfigFile -- BackendManager only calls refresh(),
    .backends and .source_path on it."""

    def __init__(self, backends=None, source_path="apython_lb.toml"):
        self.backends = backends or []
        self.source_path = source_path
        self._generation = 0

    def refresh(self) -> int:
        self._generation += 1
        return self._generation

    def set_backends(self, backends):
        self.backends = backends


def _file_backend(name="file_pool", servers=None):
    return {
        "name": name,
        "proto": "https",
        "host": "example.com",
        "servers": servers or ["1.2.3.4", "app.example.com"],
    }


@pytest.mark.asyncio
async def test_file_defined_backend_is_pooled_and_health_checkable(monkeypatch):
    monkeypatch.setattr("classes.backend_manager.broadcast_config_update", AsyncMock())
    manager = BackendManager(db=DummyDb(), config_file=FakeConfigFile([_file_backend()]))

    await manager._poll_configs_once()

    # Stored in the same 4-tuple shape the DB-backed path has always used,
    # with the servers array joined -- so _parse_servers and the health
    # check loop need no knowledge of where the config came from.
    assert manager._configs["file_pool"] == (
        "https",
        "example.com",
        "1.2.3.4,app.example.com",
        "/",
    )
    assert "file_pool" in manager._backends

    host_name, servers = await manager._parse_servers(manager._configs["file_pool"])
    assert host_name == "example.com"
    assert servers == ["https://1.2.3.4", "https://app.example.com"]


@pytest.mark.asyncio
async def test_file_and_db_backends_coexist(monkeypatch):
    monkeypatch.setattr("classes.backend_manager.broadcast_config_update", AsyncMock())
    manager = BackendManager(
        db=DuplicateNameDb([_config_row(1, "db_pool", "9.9.9.9", "2026-07-18 11:00:00")]),
        config_file=FakeConfigFile([_file_backend()]),
    )

    await manager._poll_configs_once()

    assert set(manager._configs) == {"file_pool", "db_pool"}
    assert manager.file_backend_names() == frozenset({"file_pool"})


@pytest.mark.asyncio
async def test_db_row_cannot_take_over_a_file_owned_name(monkeypatch):
    """One name, one owner -- the same rule routes already follow."""
    monkeypatch.setattr("classes.backend_manager.broadcast_config_update", AsyncMock())
    manager = BackendManager(
        db=DuplicateNameDb([_config_row(1, "file_pool", "9.9.9.9", "2026-07-18 11:00:00")]),
        config_file=FakeConfigFile([_file_backend()]),
    )

    await manager._poll_configs_once()

    # The file's definition stands; the colliding DB row is ignored.
    assert manager._configs["file_pool"][2] == "1.2.3.4,app.example.com"


@pytest.mark.asyncio
async def test_validate_new_name_rejects_names_either_source_owns(monkeypatch):
    monkeypatch.setattr("classes.backend_manager.broadcast_config_update", AsyncMock())
    manager = BackendManager(
        db=DuplicateNameDb([_config_row(1, "db_pool", "9.9.9.9", "2026-07-18 11:00:00")]),
        config_file=FakeConfigFile([_file_backend()]),
    )
    await manager._poll_configs_once()

    with pytest.raises(ValueError, match="config file"):
        manager.validate_new_name("file_pool")
    with pytest.raises(ValueError):
        manager.validate_new_name("db_pool")

    manager.validate_new_name("brand_new")  # free -- no error


@pytest.mark.asyncio
async def test_file_reload_rejected_when_it_claims_a_db_owned_name(monkeypatch):
    monkeypatch.setattr("classes.backend_manager.broadcast_config_update", AsyncMock())
    config_file = FakeConfigFile([])
    manager = BackendManager(
        db=DuplicateNameDb([_config_row(1, "db_pool", "9.9.9.9", "2026-07-18 11:00:00")]),
        config_file=config_file,
    )
    await manager._poll_configs_once()
    assert "db_pool" in manager._configs

    config_file.set_backends([_file_backend(name="db_pool")])
    with pytest.raises(ValueError, match="collide"):
        manager.reload_file()

    # Previous (valid) file view keeps serving rather than half-applying.
    assert manager.file_backend_names() == frozenset()


@pytest.mark.asyncio
async def test_removing_a_backend_from_the_file_drops_it(monkeypatch):
    monkeypatch.setattr("classes.backend_manager.broadcast_config_update", AsyncMock())
    config_file = FakeConfigFile([_file_backend()])
    manager = BackendManager(db=DummyDb(), config_file=config_file)
    await manager._poll_configs_once()
    assert "file_pool" in manager._backends

    config_file.set_backends([])
    await manager._poll_configs_once()

    assert "file_pool" not in manager._backends
    assert "file_pool" not in manager._configs


@pytest.mark.parametrize(
    "bad",
    [
        {"proto": "https", "host": "h", "servers": ["1.2.3.4"]},  # no name
        {"name": "b", "proto": "ftp", "host": "h", "servers": ["1.2.3.4"]},  # bad proto
        {"name": "b", "proto": "https", "servers": ["1.2.3.4"]},  # no host
        {"name": "b", "proto": "https", "host": "h"},  # no servers
        {"name": "b", "proto": "https", "host": "h", "servers": []},  # empty servers
        {"name": "b", "proto": "https", "host": "h", "servers": "1.2.3.4"},  # not a list
        {"name": "b", "proto": "https", "host": "h", "servers": [""]},  # empty entry
    ],
)
def test_validate_backend_fails_loud_on_invalid_entries(bad):
    with pytest.raises(ValueError):
        _validate_backend(bad)


def test_validate_backend_rejects_duplicate_names_within_the_file():
    with pytest.raises(ValueError, match="Duplicate"):
        _backends_from_raw([_file_backend(), _file_backend()], "apython_lb.toml")


def test_list_configs_tags_provenance_and_hides_shadowed_db_rows():
    manager = BackendManager(db=DummyDb(), config_file=FakeConfigFile([_file_backend()]))
    manager._file_configs = {"file_pool": ("https", "example.com", "1.2.3.4", "/")}

    items = manager.list_configs(
        [
            {
                "id": 1,
                "name": "db_pool",
                "data": ["http", "h", "9.9.9.9", "/"],
                "last_selected": "x",
            },
            {
                "id": 2,
                "name": "file_pool",
                "data": ["http", "h", "8.8.8.8", "/"],
                "last_selected": "y",
            },
        ]
    )

    by_name = {item["name"]: item for item in items}
    assert by_name["file_pool"]["source"] == "file"
    assert by_name["file_pool"]["id"] is None  # not a database row
    assert by_name["db_pool"]["source"] == "db"
    # The shadowed DB row isn't rendered twice under the same name.
    assert len(items) == 2
