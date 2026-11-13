import asyncio
import time

import pytest

from classes.route_manager import RouteEntry, RouteManager, _load_entries


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

    entries = _load_entries(file_path)

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

    entries = _load_entries(file_path)

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
        _load_entries(file_path)


def test_load_entries_fails_on_empty_routes_list(tmp_path):
    file_path = write_toml(tmp_path / "routes.toml", "routes = []\n")

    with pytest.raises(ValueError):
        _load_entries(file_path)


def test_load_entries_fails_on_missing_file(tmp_path):
    with pytest.raises(OSError):
        _load_entries(str(tmp_path / "does-not-exist.toml"))


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
    rm = RouteManager(file_path=str(file_path), poll_interval=1)
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
async def test_start_reloads_in_the_background_on_mtime_change(tmp_path):
    """Exercises the actual poll loop (not just load_now() called by hand) --
    a short poll_interval so the test doesn't have to wait long."""
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
    rm = RouteManager(file_path=str(file_path), poll_interval=0.1)
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
