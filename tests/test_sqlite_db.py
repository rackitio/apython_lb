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
