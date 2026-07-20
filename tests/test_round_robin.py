import pytest

from classes.round_robin import AsyncRoundRobin


@pytest.mark.asyncio
async def test_add_and_get_next_cycle():
    rr = AsyncRoundRobin(["a", "b"])

    assert rr.get_current() == "a"
    assert await rr.get_next() == "a"
    assert await rr.get_next() == "b"
    assert await rr.get_next() == "a"

    await rr.add(["b", "c"])
    assert await rr.get_all() == ["a", "b", "c"]
    assert rr.get_current() == "b"

    await rr.delete("a")
    assert await rr.get_all() == ["b", "c"]
    assert await rr.get_next() == "b"

    # deleting a non-existent item should not raise
    await rr.delete("nope")
    assert await rr.get_all() == ["b", "c"]
