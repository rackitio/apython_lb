import pytest

from classes.sticky_session import StickyRoundRobin


@pytest.mark.asyncio
async def test_sticky_session_pinning_and_fallback():
    rr = StickyRoundRobin(["a", "b"])

    assert await rr.get_next("session1") == "a"
    assert await rr.get_next("session1") == "a"
    assert rr.get_current("session1") == "a"

    assert await rr.get_next("session2") == "b"

    await rr.delete("a")
    assert rr.get_current("session1") == "b"
    assert await rr.get_next("session1") == "b"

    await rr.release_session("session1")
    assert await rr.get_next("session1") == "b"

    await rr.replace(["c", "d"])
    assert set(await rr.get_all()) == {"c", "d"}
    assert await rr.get_session_map() == {}


@pytest.mark.asyncio
async def test_session_map_bounded_evicts_oldest():
    """Regression: session pins grew without bound — a cookie-refusing client
    minted a new entry per request (memory-exhaustion vector)."""
    rr = StickyRoundRobin(["a", "b"], max_sessions=2)

    await rr.get_next("s1")
    await rr.get_next("s2")
    await rr.get_next("s3")  # exceeds the cap — evicts the oldest pin (s1)

    session_map = await rr.get_session_map()
    assert len(session_map) == 2
    assert "s1" not in session_map
    assert "s3" in session_map
