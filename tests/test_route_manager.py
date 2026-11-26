import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from classes.config_file import ConfigFile
from classes.route_manager import RouteEntry, RouteManager, _entries_from_raw


def _config_file(file_path) -> ConfigFile:
    # legacy_path == path so the routes.toml fallback never kicks in here;
    # these tests are about parsing and merging, not migration.
    return ConfigFile(path=str(file_path), legacy_path=str(file_path))


def load_entries(file_path):
    """Parse + validate a config file's routes the way RouteManager does."""
    config_file = _config_file(file_path)
    config_file.load()
    return _entries_from_raw(config_file.routes, str(file_path))


def manager_for(file_path, **kwargs) -> RouteManager:
    return RouteManager(config_file=_config_file(file_path), **kwargs)


class FakeDb:
    """Minimal stand-in for AsyncDb -- RouteManager only ever calls
    get_all_routes() on it."""

    def __init__(self, rows=None):
        self.rows = rows or []

    async def get_all_routes(self):
        return self.rows


def write_toml(path, content: str):
    path.write_text(content)
    return str(path)


# ── Loading / validation ─────────────────────────────────────────────────── #


def test_load_entries_parses_valid_file(tmp_path):
    file_path = write_toml(
        tmp_path / "routes.toml",
        """
        [[routes]]
        path = "/"
        backend_name = "my_backend"
        route_policy = "round_robin"

        [[routes]]
        path = "/v1/"
        backend_name = "dns_backend"
        route_policy = "sticky_ip"
        url_match = '^v1/(.+)$'
        url_rewrite = '\\1'
        """,
    )

    entries = load_entries(file_path)

    # Longest path first.
    assert [e.path for e in entries] == ["/v1/", "/"]
    assert entries[0].backend_name == "dns_backend"
    assert entries[0].url_match == r"^v1/(.+)$"
    assert entries[1].methods == frozenset({"GET", "POST"})  # default when omitted


def test_load_entries_respects_explicit_methods(tmp_path):
    file_path = write_toml(
        tmp_path / "routes.toml",
        """
        [[routes]]
        path = "/reports/"
        backend_name = "reports_backend"
        route_policy = "round_robin"
        methods = ["get"]
        """,
    )

    entries = load_entries(file_path)

    assert entries[0].methods == frozenset({"GET"})  # uppercased


@pytest.mark.parametrize(
    "bad_toml",
    [
        # path missing trailing slash
        """
        [[routes]]
        path = "/v1"
        backend_name = "b"
        route_policy = "round_robin"
        """,
        # path missing leading slash
        """
        [[routes]]
        path = "v1/"
        backend_name = "b"
        route_policy = "round_robin"
        """,
        # invalid route_policy
        """
        [[routes]]
        path = "/"
        backend_name = "b"
        route_policy = "most_requests"
        """,
        # invalid url_match regex
        """
        [[routes]]
        path = "/"
        backend_name = "b"
        route_policy = "round_robin"
        url_match = "("
        """,
        # invalid method name
        """
        [[routes]]
        path = "/"
        backend_name = "b"
        route_policy = "round_robin"
        methods = ["FETCH"]
        """,
        # empty methods list
        """
        [[routes]]
        path = "/"
        backend_name = "b"
        route_policy = "round_robin"
        methods = []
        """,
        # missing backend_name
        """
        [[routes]]
        path = "/"
        route_policy = "round_robin"
        """,
        # duplicate path
        """
        [[routes]]
        path = "/"
        backend_name = "a"
        route_policy = "round_robin"

        [[routes]]
        path = "/"
        backend_name = "b"
        route_policy = "round_robin"
        """,
    ],
)
def test_load_entries_fails_loud_on_invalid_config(tmp_path, bad_toml):
    file_path = write_toml(tmp_path / "routes.toml", bad_toml)

    with pytest.raises(ValueError):
        load_entries(file_path)


def test_load_entries_fails_on_empty_routes_list(tmp_path):
    file_path = write_toml(tmp_path / "routes.toml", "routes = []\n")

    with pytest.raises(ValueError):
        load_entries(file_path)


def test_load_entries_fails_on_missing_file(tmp_path):
    with pytest.raises(OSError):
        load_entries(str(tmp_path / "does-not-exist.toml"))


# ── Matching ──────────────────────────────────────────────────────────────── #

ROOT = RouteEntry(path="/", backend_name="root_pool", route_policy="round_robin")
V1 = RouteEntry(path="/v1/", backend_name="v1_pool", route_policy="round_robin")
V1_SPECIAL = RouteEntry(
    path="/v1/special/", backend_name="special_pool", route_policy="round_robin"
)
READ_ONLY = RouteEntry(
    path="/reports/",
    backend_name="reports_pool",
    route_policy="round_robin",
    methods=frozenset({"GET"}),
)


def _manager_with(*entries: RouteEntry) -> RouteManager:
    rm = RouteManager.__new__(RouteManager)  # skip __init__'s file/env setup
    rm._entries = sorted(entries, key=lambda e: len(e.path), reverse=True)
    return rm


def test_match_picks_longest_prefix():
    rm = _manager_with(ROOT, V1, V1_SPECIAL)

    entry, allowed = rm.match("/v1/special/thing", "GET")
    assert entry is V1_SPECIAL
    assert allowed

    entry, allowed = rm.match("/v1/other", "GET")
    assert entry is V1
    assert allowed

    entry, allowed = rm.match("/anything-else", "GET")
    assert entry is ROOT
    assert allowed


def test_match_is_segment_safe_not_substring():
    """Regression guard: '/v1' must not match a '/v1xyz' path just because
    it's a string prefix -- paths are normalized with a trailing slash
    specifically to prevent this."""
    rm = _manager_with(ROOT, V1)

    entry, _ = rm.match("/v123abc", "GET")
    assert entry is ROOT  # not V1


def test_match_bare_path_without_trailing_slash_still_matches():
    """'/v1' (no trailing slash) should match the '/v1/' entry directly,
    the way Werkzeug's own redirect used to handle it when '/v1/' was a
    literal registered route."""
    rm = _manager_with(ROOT, V1)

    entry, _ = rm.match("/v1", "GET")
    assert entry is V1


def test_match_returns_none_when_no_path_configured():
    rm = _manager_with(V1)

    entry, allowed = rm.match("/unmatched", "GET")
    assert entry is None
    assert allowed is False


def test_match_returns_entry_with_method_not_allowed():
    rm = _manager_with(ROOT, READ_ONLY)

    entry, allowed = rm.match("/reports/q1", "POST")
    assert entry is READ_ONLY
    assert allowed is False


# ── Hot reload ────────────────────────────────────────────────────────────── #


def test_load_now_picks_up_file_changes(tmp_path):
    file_path = tmp_path / "routes.toml"
    write_toml(
        file_path,
        """
        [[routes]]
        path = "/"
        backend_name = "pool_a"
        route_policy = "round_robin"
        """,
    )
    rm = manager_for(file_path, poll_interval=1)
    rm.load_now()
    assert rm.match("/x", "GET")[0].backend_name == "pool_a"

    # mtime granularity on some filesystems is 1s -- make sure the new write
    # lands in a different mtime bucket than the first.
    time.sleep(1.1)
    write_toml(
        file_path,
        """
        [[routes]]
        path = "/"
        backend_name = "pool_b"
        route_policy = "round_robin"
        """,
    )
    rm.load_now()  # simulate what start()'s poll loop does on an mtime change

    assert rm.match("/x", "GET")[0].backend_name == "pool_b"


@pytest.mark.asyncio
async def test_start_reloads_in_the_background_on_mtime_change(tmp_path, monkeypatch):
    """Exercises the actual poll loop (not just load_now() called by hand) --
    a short poll_interval so the test doesn't have to wait long.

    start() broadcasts on change, which needs a Quart app context in
    production (provided by app.add_background_task() -- see main.py) but
    not here, so the broadcast call itself is mocked out, same convention
    test_backend_manager.py already uses for its own poll loop."""
    monkeypatch.setattr("classes.route_manager.broadcast_route_update", AsyncMock())

    file_path = tmp_path / "routes.toml"
    write_toml(
        file_path,
        """
        [[routes]]
        path = "/"
        backend_name = "pool_a"
        route_policy = "round_robin"
        """,
    )
    rm = manager_for(file_path, poll_interval=0.1)
    rm.load_now()

    task = asyncio.ensure_future(rm.start())
    try:
        time.sleep(1.1)  # cross into a new mtime second bucket
        write_toml(
            file_path,
            """
            [[routes]]
            path = "/"
            backend_name = "pool_b"
            route_policy = "round_robin"
            """,
        )

        for _ in range(50):  # up to ~1s of polling at 0.1s intervals
            if rm.match("/x", "GET")[0].backend_name == "pool_b":
                break
            await asyncio.sleep(0.05)

        assert rm.match("/x", "GET")[0].backend_name == "pool_b"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ── DB-sourced routes (Phase 2) ──────────────────────────────────────────── #

DB_ROW = {
    "id": 1,
    "path": "/v2/",
    "backend_name": "v2_backend",
    "route_policy": "round_robin",
    "url_match": None,
    "url_rewrite": None,
    "methods": ["GET", "POST"],
}


@pytest.mark.asyncio
async def test_reload_db_merges_entries_into_match(tmp_path):
    file_path = write_toml(
        tmp_path / "routes.toml",
        """
        [[routes]]
        path = "/"
        backend_name = "root_pool"
        route_policy = "round_robin"
        """,
    )
    rm = manager_for(file_path, db=FakeDb([DB_ROW]))
    rm.load_now()

    changed = await rm.reload_db()
    assert changed is True

    entry, allowed = rm.match("/v2/thing", "GET")
    assert entry.backend_name == "v2_backend"
    assert entry.source == "db"
    assert entry.db_id == 1
    assert allowed

    # Root route from the file is still there, just less specific.
    assert rm.match("/other", "GET")[0].backend_name == "root_pool"


@pytest.mark.asyncio
async def test_reload_db_returns_false_when_nothing_changed(tmp_path):
    file_path = write_toml(
        tmp_path / "routes.toml",
        """
        [[routes]]
        path = "/"
        backend_name = "root_pool"
        route_policy = "round_robin"
        """,
    )
    fake_db = FakeDb([DB_ROW])
    rm = manager_for(file_path, db=fake_db)
    rm.load_now()

    assert await rm.reload_db() is True
    assert await rm.reload_db() is False  # same rows, nothing to do

    fake_db.rows = []
    assert await rm.reload_db() is True  # row removed -- change detected
    assert rm.match("/v2/thing", "GET")[0].backend_name == "root_pool"


@pytest.mark.asyncio
async def test_reload_db_skips_invalid_row_and_keeps_rest(tmp_path):
    file_path = write_toml(
        tmp_path / "routes.toml",
        """
        [[routes]]
        path = "/"
        backend_name = "root_pool"
        route_policy = "round_robin"
        """,
    )
    bad_row = dict(DB_ROW, id=2, path="/bad/", route_policy="most_requests")
    rm = manager_for(file_path, db=FakeDb([DB_ROW, bad_row]))
    rm.load_now()
    await rm.reload_db()

    assert rm.match("/v2/thing", "GET")[0].backend_name == "v2_backend"
    assert rm.match("/bad/thing", "GET")[0].backend_name == "root_pool"  # skipped


@pytest.mark.asyncio
async def test_reload_db_skips_row_colliding_with_file_path(tmp_path):
    file_path = write_toml(
        tmp_path / "routes.toml",
        """
        [[routes]]
        path = "/v2/"
        backend_name = "file_pool"
        route_policy = "round_robin"
        """,
    )
    colliding_row = dict(DB_ROW, path="/v2/")
    rm = manager_for(file_path, db=FakeDb([colliding_row]))
    rm.load_now()
    await rm.reload_db()

    # The file-sourced entry wins; the colliding DB row never gets merged in.
    entry, _ = rm.match("/v2/thing", "GET")
    assert entry.backend_name == "file_pool"
    assert entry.source == "file"


def test_validate_new_path_rejects_existing_file_and_db_paths():
    rm = RouteManager.__new__(RouteManager)
    rm._file_entries = [ROOT]
    rm._db_entries = [
        RouteEntry(path="/v2/", backend_name="x", route_policy="round_robin", source="db")
    ]
    rm._rebuild()

    with pytest.raises(ValueError):
        rm.validate_new_path("/")
    with pytest.raises(ValueError):
        rm.validate_new_path("/v2/")
    rm.validate_new_path("/v3/")  # free -- no error


def test_load_now_rejects_file_path_colliding_with_db_entry(tmp_path):
    file_path = write_toml(
        tmp_path / "routes.toml",
        """
        [[routes]]
        path = "/v2/"
        backend_name = "file_pool"
        route_policy = "round_robin"
        """,
    )
    rm = manager_for(file_path)
    rm._db_entries = [
        RouteEntry(path="/v2/", backend_name="x", route_policy="round_robin", source="db")
    ]

    with pytest.raises(ValueError):
        rm.load_now()
