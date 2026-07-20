import pytest

import classes.ip_tracker as ip_tracker_module
from classes.ip_tracker import IPTracker


@pytest.mark.asyncio
async def test_record_404_and_penalty_cycle(monkeypatch):
    current_time = 1000.0

    def fake_time():
        return current_time

    monkeypatch.setattr(ip_tracker_module.time, "monotonic", fake_time)

    tracker = IPTracker(threshold=2, window=10, duration=30)

    assert not await tracker.record_404("1.2.3.4")
    current_time += 1
    assert await tracker.record_404("1.2.3.4")

    assert await tracker.is_penalised("1.2.3.4")
    penalty = await tracker.get_penalty("1.2.3.4")
    assert penalty["ip"] == "1.2.3.4"
    assert penalty["remaining_seconds"] <= 30

    counts = await tracker.get_404_count("1.2.3.4")
    assert counts["window_count"] == 2
    assert counts["lifetime_count"] == 2

    await tracker.add_penalty("2.2.2.2", duration=5)
    assert await tracker.is_penalised("2.2.2.2")

    current_time += 31
    assert not await tracker.is_penalised("1.2.3.4")
    assert await tracker.get_penalty("1.2.3.4") is None

    await tracker.update_config(threshold=5, window=20, duration=10)
    assert tracker.threshold == 5
    assert tracker.window == 20
    assert tracker.duration == 10

    await tracker.reset_404_count("1.2.3.4")
    counts = await tracker.get_404_count("1.2.3.4")
    assert counts["window_count"] == 0

    await tracker.delete("2.2.2.2")
    assert not await tracker.is_penalised("2.2.2.2")

    await tracker.clear_all()
    state = await tracker.get_all()
    assert state["404_counts"] == {}
    assert state["penalised"] == {}


@pytest.mark.asyncio
async def test_sweep_removes_idle_ips_and_expired_penalties():
    """Regression: 404 logs, totals, and expired penalties were never pruned."""
    import time

    tracker = IPTracker(threshold=10, window=1, duration=100, sweep_every=2)
    now = time.monotonic()

    await tracker.record_404("1.1.1.1")
    tracker._404_log["1.1.1.1"] = [now - 5]  # stale window
    tracker._penalties["9.9.9.9"] = now - 1  # expired penalty
    tracker._penalties["3.3.3.3"] = now + 100  # active penalty
    tracker._404_log["3.3.3.3"] = [now - 5]  # stale log but still penalised

    await tracker.record_404("2.2.2.2")  # second 404 triggers the sweep

    assert "1.1.1.1" not in tracker._404_log
    assert "1.1.1.1" not in tracker._404_totals
    assert "9.9.9.9" not in tracker._penalties
    # Penalised IPs are kept even with a stale 404 window
    assert "3.3.3.3" in tracker._penalties
    assert await tracker.is_penalised("3.3.3.3")
