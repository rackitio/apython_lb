import time

import pytest

from classes.config_file import ConfigFile

UNIFIED = """
[[backends]]
name = "pool_a"
proto = "https"
host = "example.com"
servers = ["1.2.3.4", "app.example.com"]

[[routes]]
path = "/"
backend_name = "pool_a"
route_policy = "round_robin"
"""


def write(path, content: str):
    path.write_text(content)
    return path


def test_loads_both_sections(tmp_path):
    path = write(tmp_path / "apython_lb.toml", UNIFIED)
    config_file = ConfigFile(path=str(path), legacy_path=str(tmp_path / "routes.toml"))

    config_file.load()

    assert [b["name"] for b in config_file.backends] == ["pool_a"]
    assert config_file.backends[0]["servers"] == ["1.2.3.4", "app.example.com"]
    assert [r["path"] for r in config_file.routes] == ["/"]
    assert config_file.source_path == str(path)


def test_routes_only_and_backends_only_are_both_valid(tmp_path):
    """Routes can come from the API and backends from the file (or vice
    versa), so a file carrying only one of the two sections is legitimate."""
    routes_only = write(
        tmp_path / "routes_only.toml",
        '[[routes]]\npath = "/"\nbackend_name = "b"\nroute_policy = "round_robin"\n',
    )
    backends_only = write(
        tmp_path / "backends_only.toml",
        '[[backends]]\nname = "b"\nproto = "http"\nhost = "h"\nservers = ["1.2.3.4"]\n',
    )

    first = ConfigFile(path=str(routes_only), legacy_path=str(routes_only))
    first.load()
    assert first.routes and first.backends == []

    second = ConfigFile(path=str(backends_only), legacy_path=str(backends_only))
    second.load()
    assert second.backends and second.routes == []


def test_refresh_bumps_generation_only_when_the_file_changes(tmp_path):
    """The generation counter is what lets two managers share one reader --
    each notices a change exactly once regardless of who triggered it."""
    path = write(tmp_path / "apython_lb.toml", UNIFIED)
    config_file = ConfigFile(path=str(path), legacy_path=str(path))

    first = config_file.refresh()
    assert first == 1
    assert config_file.refresh() == first  # unchanged file, no reparse

    time.sleep(1.1)  # some filesystems only keep whole-second mtimes
    write(path, UNIFIED.replace("pool_a", "pool_b"))

    assert config_file.refresh() == first + 1
    assert config_file.backends[0]["name"] == "pool_b"


def test_missing_file_raises(tmp_path):
    config_file = ConfigFile(
        path=str(tmp_path / "nope.toml"), legacy_path=str(tmp_path / "also-nope.toml")
    )

    with pytest.raises(OSError):
        config_file.load()


def test_malformed_toml_raises(tmp_path):
    path = write(tmp_path / "apython_lb.toml", "[[routes]\nbroken")
    config_file = ConfigFile(path=str(path), legacy_path=str(path))

    with pytest.raises(Exception):
        config_file.load()


def test_file_with_no_recognised_sections_raises(tmp_path):
    path = write(tmp_path / "apython_lb.toml", 'unrelated = "value"\n')
    config_file = ConfigFile(path=str(path), legacy_path=str(path))

    with pytest.raises(ValueError):
        config_file.load()


# ── legacy routes.toml fallback ──────────────────────────────────────────── #


def test_legacy_routes_file_is_used_and_warns(tmp_path, caplog):
    legacy = write(
        tmp_path / "routes.toml",
        '[[routes]]\npath = "/legacy/"\nbackend_name = "b"\nroute_policy = "round_robin"\n',
    )
    config_file = ConfigFile(path=str(tmp_path / "apython_lb.toml"), legacy_path=str(legacy))

    with caplog.at_level("WARNING"):
        config_file.load()

    assert [r["path"] for r in config_file.routes] == ["/legacy/"]
    assert config_file.backends == []
    assert config_file.source_path == str(legacy)
    assert "deprecated" in caplog.text.lower()


def test_legacy_file_wins_over_the_shipped_default(tmp_path):
    """The image bakes in apython_lb.toml, so its presence says nothing about
    intent -- a routes.toml only exists because someone mounted one. If the
    modern path won here, mounting an existing routes.toml would silently
    serve the image's defaults instead of the operator's config."""
    modern = write(tmp_path / "apython_lb.toml", UNIFIED)
    legacy = write(
        tmp_path / "routes.toml",
        '[[routes]]\npath = "/mounted/"\nbackend_name = "b"\nroute_policy = "round_robin"\n',
    )
    config_file = ConfigFile(path=str(modern), legacy_path=str(legacy))

    config_file.load()

    assert [r["path"] for r in config_file.routes] == ["/mounted/"]


def test_legacy_file_defining_backends_is_refused(tmp_path):
    """[[backends]] in a file being read as legacy means the operator renamed
    nothing and expects backends to work -- fail loud instead of ignoring."""
    legacy = write(
        tmp_path / "routes.toml",
        '[[backends]]\nname = "b"\nproto = "http"\nhost = "h"\nservers = ["1.2.3.4"]\n',
    )
    config_file = ConfigFile(path=str(tmp_path / "apython_lb.toml"), legacy_path=str(legacy))

    with pytest.raises(ValueError, match="Rename it"):
        config_file.load()
