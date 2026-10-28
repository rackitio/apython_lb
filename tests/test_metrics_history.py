import asyncio

import pytest
from prometheus_client import CollectorRegistry, Counter, Gauge

import classes.metrics_history as mh_module
from classes.metrics_history import MetricsHistory, _render_sparkline


def _history_with_metrics(counter, gauge):
    """Builds a MetricsHistory wired to throwaway Counter/Gauge instances
    (not metrics.py's real, shared ones) so tests don't pollute or depend on
    global metric state. Returns (history, original_tracked, mh_module) --
    callers must restore mh_module._TRACKED = original_tracked when done,
    since _TRACKED is a module-level global sample_now()/render_panels()
    read at call time."""
    history = MetricsHistory()
    original = mh_module._TRACKED
    mh_module._TRACKED = [
        (gauge, "backend_healthy_servers", "gauge", "healthy servers", "#2a9d2a"),
        (counter, "lb_retries_total", "counter", "retries / interval", "#e0a82e"),
    ]
    return history, original, mh_module


@pytest.fixture
def throwaway_metrics():
    registry = CollectorRegistry()
    counter = Counter("lb_retries_total", "test", ["backend_name"], registry=registry)
    gauge = Gauge("backend_healthy_servers", "test", ["backend_name"], registry=registry)
    yield counter, gauge


def test_sample_now_records_gauge_raw_value(throwaway_metrics):
    counter, gauge = throwaway_metrics
    gauge.labels(backend_name="my_backend").set(2)

    history, original, mh_module = _history_with_metrics(counter, gauge)
    try:
        history.sample_now()
        key = ("backend_healthy_servers", (("backend_name", "my_backend"),))
        assert list(history._series[key])[-1][1] == 2.0
    finally:
        mh_module._TRACKED = original


def test_sample_now_first_observation_reports_full_value_not_zero(throwaway_metrics):
    """Regression: the counter is born at 0 alongside this sampler (it's
    process-local, never pre-seeded), so "no prior sample" must mean "this
    much happened since we started watching" -- not get reported as 0. A
    backend failing for the very first time, incrementing a counter that's
    never been touched before, is exactly the burst this dashboard exists to
    show; silently reporting it as zero defeats the whole point."""
    counter, gauge = throwaway_metrics
    history, original, mh_module = _history_with_metrics(counter, gauge)
    try:
        counter.labels(backend_name="my_backend").inc(3)
        history.sample_now()  # first-ever sample for this series

        key = ("lb_retries_total", (("backend_name", "my_backend"),))
        assert list(history._series[key])[-1][1] == 3.0
    finally:
        mh_module._TRACKED = original


def test_sample_now_computes_counter_delta_not_cumulative(throwaway_metrics):
    counter, gauge = throwaway_metrics
    history, original, mh_module = _history_with_metrics(counter, gauge)
    try:
        counter.labels(backend_name="my_backend").inc(3)
        history.sample_now()  # first sample: reports the full 3 (see test above)
        counter.labels(backend_name="my_backend").inc(2)
        history.sample_now()  # second sample: delta is 2, not the cumulative 5

        key = ("lb_retries_total", (("backend_name", "my_backend"),))
        values = [v for _, v in history._series[key]]
        assert values == [3.0, 2.0]
    finally:
        mh_module._TRACKED = original


def test_sample_now_ignores_created_sample(throwaway_metrics):
    """Counters auto-expose a _created timestamp sample alongside the real
    value -- it must not show up as its own (bogus) series."""
    counter, gauge = throwaway_metrics
    counter.labels(backend_name="my_backend").inc()

    history, original, mh_module = _history_with_metrics(counter, gauge)
    try:
        history.sample_now()
        assert all(not name.endswith("_created") for name, _labels in history._series)
    finally:
        mh_module._TRACKED = original


def test_render_panels_groups_by_backend_name(throwaway_metrics):
    """render_panels() returns one group per backend_name (each holding its
    own row of panels), not a flat list -- so each backend's charts render
    together as a row instead of wrapping arbitrarily across backends."""
    counter, gauge = throwaway_metrics
    gauge.labels(backend_name="my_backend").set(2)
    gauge.labels(backend_name="dns_backend").set(1)
    counter.labels(backend_name="my_backend").inc()

    history, original, mh_module = _history_with_metrics(counter, gauge)
    try:
        history.sample_now()
        groups = history.render_panels()
        by_name = {g["backend_name"]: g["panels"] for g in groups}

        assert set(by_name) == {"my_backend", "dns_backend"}

        my_titles = {p["title"] for p in by_name["my_backend"]}
        assert "healthy servers" in my_titles
        assert "retries / interval" in my_titles

        # dns_backend never had a retry -- its group still has a "retries"
        # panel (rendered as "no data yet"), it just has an empty series.
        dns_titles = {p["title"] for p in by_name["dns_backend"]}
        assert "healthy servers" in dns_titles
        assert "retries / interval" in dns_titles
    finally:
        mh_module._TRACKED = original


def test_render_panels_empty_when_no_backends_seen():
    history = MetricsHistory()
    assert history.render_panels() == []


@pytest.mark.asyncio
async def test_start_invokes_on_sample_callback_each_tick():
    history = MetricsHistory(interval=0.01)
    calls = []

    async def on_sample():
        calls.append(1)
        if len(calls) >= 3:
            raise asyncio.CancelledError  # stop the loop cleanly for the test

    with pytest.raises(asyncio.CancelledError):
        await history.start(on_sample=on_sample)

    assert len(calls) == 3


@pytest.mark.asyncio
async def test_start_survives_on_sample_exceptions():
    """A broadcast failure (e.g. a client send error bubbling up) must not
    kill the whole background sampling loop."""
    history = MetricsHistory(interval=0.01)
    calls = []

    async def flaky_on_sample():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        if len(calls) >= 2:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await history.start(on_sample=flaky_on_sample)

    assert len(calls) == 2  # the RuntimeError on call 1 didn't stop call 2


# ── _render_sparkline ─────────────────────────────────────────────────── #


def test_render_sparkline_empty_points():
    svg = _render_sparkline([])
    assert "no data yet" in svg
    assert "<svg" in svg


def test_render_sparkline_flat_series_no_divide_by_zero():
    svg = _render_sparkline([(1.0, 5.0), (2.0, 5.0), (3.0, 5.0)])
    assert "<polyline" in svg
    assert "5" in svg  # latest value label


def test_render_sparkline_normal_series():
    svg = _render_sparkline([(1.0, 0.0), (2.0, 5.0), (3.0, 2.0)])
    assert "<polyline" in svg
    assert "points=" in svg
    assert svg.count(",") >= 3  # at least 3 coordinate pairs
