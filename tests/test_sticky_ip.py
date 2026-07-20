import pytest

from classes.sticky_ip import StickyIP


class DummyRoundRobin:
    def __init__(self, backend):
        self.backend = backend

    async def get_next(self, session_id=None):
        return self.backend


@pytest.mark.asyncio
async def test_pin_get_next_and_release():
    sticky = StickyIP()
    rr = DummyRoundRobin("backendA")

    assert await sticky.get_next("1.1.1.1", rr) == "backendA"
    assert await sticky.get_pinned("1.1.1.1") == "backendA"
    assert await sticky.get_next("1.1.1.1", rr) == "backendA"

    await sticky.release("1.1.1.1")
    assert await sticky.get_pinned("1.1.1.1") is None


@pytest.mark.asyncio
async def test_delete_backend_clears_pins_and_clear():
    sticky = StickyIP()
    await sticky.pin("1.1.1.1", "backendA")
    await sticky.pin("2.2.2.2", "backendA")

    stale = await sticky.delete_backend("backendA")
    assert set(stale) == {"1.1.1.1", "2.2.2.2"}
    assert await sticky.get_all() == {}

    await sticky.pin("3.3.3.3", "backendB")
    await sticky.clear()
    assert await sticky.get_all() == {}


@pytest.mark.asyncio
async def test_ip_map_bounded_evicts_oldest():
    """Regression: IP pins were only removed when a backend died, so the map
    grew without bound as new client IPs appeared."""
    sticky = StickyIP(max_entries=2)

    await sticky.pin("1.1.1.1", "a")
    await sticky.pin("2.2.2.2", "b")
    await sticky.pin("3.3.3.3", "a")  # exceeds the cap — evicts 1.1.1.1

    pins = await sticky.get_all()
    assert len(pins) == 2
    assert "1.1.1.1" not in pins

    # Re-pinning an existing IP must not evict anyone
    await sticky.pin("2.2.2.2", "c")
    pins = await sticky.get_all()
    assert len(pins) == 2
    assert pins["2.2.2.2"] == "c"
