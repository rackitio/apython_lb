from pathlib import Path

import pytest

from classes.sqlite_db import AsyncDb


@pytest.mark.asyncio
async def test_sqlite_db_crud_operations(tmp_path: Path):
    db_path = tmp_path / "test.db"
    db = AsyncDb(str(db_path))
    await db.initialize()

    await db.insert_config("test-config", ["https", "example.com", "1.2.3.4", "/health"])
    configs = await db.get_all_configs()

    assert len(configs) == 1
    assert configs[0]["name"] == "test-config"
    assert configs[0]["data"] == ["https", "example.com", "1.2.3.4", "/health"]

    row = await db.get_config_by_name("test-config")
    assert row is not None
    assert row["name"] == "test-config"

    await db.update_last_selected(row["id"])
    await db.delete_config(row["id"])

    deleted = await db.get_config_by_name("test-config")
    assert deleted is None

    await db.db.close()


@pytest.mark.asyncio
async def test_get_all_configs_orders_most_recently_selected_first(tmp_path: Path):
    """The poll loop's last-config-wins rule relies on this DESC ordering."""
    db = AsyncDb(str(tmp_path / "test.db"))
    await db.initialize()

    await db.insert_config("first", ["https", "example.com", "1.1.1.1", "/health"])
    await db.insert_config("second", ["https", "example.com", "2.2.2.2", "/health"])

    configs = await db.get_all_configs()
    assert [c["name"] for c in configs] == ["second", "first"]

    # Selecting an older config bumps it to the front (it becomes the winner)
    await db.update_last_selected(configs[1]["id"])
    configs = await db.get_all_configs()
    assert [c["name"] for c in configs] == ["first", "second"]

    await db.db.close()


@pytest.mark.asyncio
async def test_sqlite_db_route_crud_operations(tmp_path: Path):
    db = AsyncDb(str(tmp_path / "test.db"))
    await db.initialize()

    await db.insert_route("/v2/", "v2_backend", "round_robin", "^v2/?(.*)$", r"\1", ["GET", "POST"])
    routes = await db.get_all_routes()

    assert len(routes) == 1
    assert routes[0]["path"] == "/v2/"
    assert routes[0]["backend_name"] == "v2_backend"
    assert routes[0]["route_policy"] == "round_robin"
    assert routes[0]["url_match"] == "^v2/?(.*)$"
    assert routes[0]["url_rewrite"] == r"\1"
    assert routes[0]["methods"] == ["GET", "POST"]

    await db.delete_route(routes[0]["id"])
    assert await db.get_all_routes() == []

    await db.db.close()


@pytest.mark.asyncio
async def test_sqlite_db_insert_route_rejects_duplicate_path(tmp_path: Path):
    """The Routes table's UNIQUE constraint on Path is the defense-in-depth
    backstop behind RouteManager.validate_new_path()'s own check."""
    import aiosqlite

    db = AsyncDb(str(tmp_path / "test.db"))
    await db.initialize()

    await db.insert_route("/v2/", "v2_backend", "round_robin", None, None, ["GET"])
    with pytest.raises(aiosqlite.IntegrityError):
        await db.insert_route("/v2/", "other_backend", "round_robin", None, None, ["GET"])

    await db.db.close()
