import pytest
from quart import Quart

import classes.rate_limiter as rate_limiter_module
from classes.rate_limiter import RateLimiter
from decorators.rate_limit import rate_limit


@pytest.mark.asyncio
async def test_limit_enforcement_and_window_expiry(monkeypatch):
    current_time = 1000.0

    def fake_time():
        return current_time

    monkeypatch.setattr(rate_limiter_module.time, "monotonic", fake_time)

    limiter = RateLimiter(limit=2, window=10)

    allowed, retry_after = await limiter.hit("1.2.3.4")
    assert allowed and retry_after == 0.0

    current_time += 1
    allowed, _ = await limiter.hit("1.2.3.4")
    assert allowed

    # Third request inside the window is rejected
    current_time += 1
    allowed, retry_after = await limiter.hit("1.2.3.4")
    assert not allowed
    # Oldest hit was at t=1000, window=10 -> capacity frees at t=1010 (8s from t=1002)
    assert retry_after == 8.0

    # Other IPs are unaffected
    allowed, _ = await limiter.hit("5.6.7.8")
    assert allowed

    usage = await limiter.get_usage("1.2.3.4")
    assert usage["window_count"] == 2
    assert usage["remaining"] == 0

    # After the window slides past the oldest hit, requests are allowed again
    current_time += 9
    allowed, _ = await limiter.hit("1.2.3.4")
    assert allowed


@pytest.mark.asyncio
async def test_config_update_reset_and_clear(monkeypatch):
    limiter = RateLimiter(limit=1, window=10)

    allowed, _ = await limiter.hit("1.2.3.4")
    assert allowed
    allowed, _ = await limiter.hit("1.2.3.4")
    assert not allowed

    await limiter.update_config(limit=5, window=20)
    assert limiter.limit == 5
    assert limiter.window == 20
    allowed, _ = await limiter.hit("1.2.3.4")
    assert allowed

    await limiter.reset("1.2.3.4")
    usage = await limiter.get_usage("1.2.3.4")
    assert usage["window_count"] == 0

    await limiter.hit("1.2.3.4")
    await limiter.hit("5.6.7.8")
    snapshot = await limiter.get_all()
    assert set(snapshot["usage"]) == {"1.2.3.4", "5.6.7.8"}

    await limiter.clear_all()
    snapshot = await limiter.get_all()
    assert snapshot["usage"] == {}


@pytest.mark.asyncio
async def test_rate_limit_decorator_returns_429_with_retry_after():
    app = Quart(__name__)
    app.rate_limiter = RateLimiter(limit=2, window=60)

    @app.route("/")
    @rate_limit
    async def index():
        return "ok"

    @app.route("/strict")
    @rate_limit(limit=1, window=60)
    async def strict():
        return "ok"

    client = app.test_client()

    assert (await client.get("/")).status_code == 200
    assert (await client.get("/")).status_code == 200

    response = await client.get("/")
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) >= 1

    # Route-level limiter is independent of the app-wide one
    assert (await client.get("/strict")).status_code == 200
    assert (await client.get("/strict")).status_code == 429


@pytest.mark.asyncio
async def test_sweep_removes_idle_ips():
    """Regression: idle-IP entries were never removed, growing memory forever."""
    import time

    limiter = RateLimiter(limit=5, window=1, sweep_every=2)

    await limiter.hit("1.1.1.1")
    limiter._hits["1.1.1.1"] = [time.monotonic() - 5]  # age the entry out

    await limiter.hit("2.2.2.2")  # second hit triggers the sweep

    assert "1.1.1.1" not in limiter._hits
    assert "2.2.2.2" in limiter._hits
