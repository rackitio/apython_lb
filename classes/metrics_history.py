import asyncio
import logging
import os
import time
from collections import deque
from collections.abc import Awaitable, Callable

from metrics import (
    BACKEND_HEALTHY_SERVERS,
    LB_NO_HEALTHY_BACKENDS_TOTAL,
    LB_RETRIES_TOTAL,
)

logger = logging.getLogger(__name__)

METRICS_HISTORY_INTERVAL = float(os.environ.get("METRICS_HISTORY_INTERVAL", 5))  # seconds
# ~15 minutes of history at the 5s default -- enough to see an incident
# develop and resolve without the in-memory buffer growing unbounded.
METRICS_HISTORY_POINTS = int(os.environ.get("METRICS_HISTORY_POINTS", 180))

# Which Prometheus metrics (metrics.py) the management UI graphs, and how a
# raw sample becomes a graphable point. Deliberately limited to metrics
# labeled by backend_name only (no second label dimension) for v1 -- adding
# e.g. lb_backend_errors_total (also labeled by error_type) would need
# aggregating multiple label combinations per backend_name, which isn't
# built yet. Chosen because this exact trio is what would have shown
# today's incident live: a pool draining to zero healthy servers, alongside
# the retries and pool-exhaustion events that caused it.
#   "gauge"   series are graphed as their raw value over time.
#   "counter" series are graphed as the delta since the previous sample (a
#             per-interval rate) -- an ever-climbing cumulative total isn't
#             a useful shape to look at live; a spike that returns to flat
#             is what a human actually needs to see.
#
# sample_name is the literal name each metric's values are exposed under via
# .collect() -- verified empirically against the pinned prometheus_client
# version that this is the metric's full declared name (e.g.
# "lb_retries_total"), NOT the "_total"-stripped name .collect()'s own family
# object reports in its .name field. Hardcoded here (rather than introspected
# off the metric object's private _name attribute) since both of these
# Counters already declare their name with "_total" included in metrics.py.
_TRACKED = [
    (BACKEND_HEALTHY_SERVERS, "backend_healthy_servers", "gauge", "healthy servers", "#2a9d2a"),
    (LB_RETRIES_TOTAL, "lb_retries_total", "counter", "retries / interval", "#e0a82e"),
    (
        LB_NO_HEALTHY_BACKENDS_TOTAL,
        "lb_no_healthy_backends_total",
        "counter",
        "pool-exhausted events / interval",
        "#c0392b",
    ),
]

SeriesKey = tuple[str, tuple[tuple[str, str], ...]]


def _render_sparkline(
    points: list[tuple[float, float]], width: int = 280, height: int = 50, color: str = "#2a9d2a"
) -> str:
    """
    Renders `points` (oldest-first [(timestamp, value), ...]) as an inline
    SVG sparkline string. No client-side charting library involved -- this
    is plain string-built SVG, safe to embed directly in server-rendered
    HTML (the caller must mark it `| safe` in the Jinja template; it is not
    HTML-escaped here because it needs to render as markup, not text).
    """
    if not points:
        return (
            f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
            f'class="sparkline sparkline-empty">'
            f'<text x="4" y="{height // 2}" font-size="11" fill="#999">no data yet</text>'
            f"</svg>"
        )

    values = [v for _, v in points]
    lo, hi = min(values), max(values)
    span = hi - lo or 1.0  # flat series: avoid a divide-by-zero, draw a straight line instead

    n = len(points)
    step = width / max(n - 1, 1)
    pad = 2  # px, top and bottom, so a value at the min/max isn't drawn on the very edge

    coords = []
    for i, v in enumerate(values):
        x = i * step
        y = height - pad - ((v - lo) / span) * (height - 2 * pad)
        coords.append(f"{x:.1f},{y:.1f}")

    latest = values[-1]
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" class="sparkline">'
        f'<polyline points="{" ".join(coords)}" fill="none" stroke="{color}" stroke-width="1.5"/>'
        f"</svg>"
        f'<span class="sparkline-latest">{latest:g}</span>'
    )


class MetricsHistory:
    """
    Samples a curated set of Prometheus metrics (see _TRACKED above) directly
    from the in-process registry -- no HTTP call, not even to this app's own
    /metrics -- on a fixed interval, keeping a bounded rolling window per
    label series. This is what lets the management UI show live sparklines
    without standing up an external Prometheus/Grafana stack: the data never
    leaves the process.
    """

    def __init__(
        self,
        interval: float = METRICS_HISTORY_INTERVAL,
        points: int = METRICS_HISTORY_POINTS,
    ):
        self.interval = interval
        self.points = points
        self._series: dict[SeriesKey, deque[tuple[float, float]]] = {}
        # Last raw cumulative value per counter series, to compute deltas.
        self._last_raw: dict[SeriesKey, float] = {}

    def _series_for(self, key: SeriesKey) -> deque:
        series = self._series.get(key)
        if series is None:
            series = deque(maxlen=self.points)
            self._series[key] = series
        return series

    def sample_now(self) -> None:
        """Takes one sample of every tracked metric's current value(s) and
        appends to each series' rolling window. Safe to call concurrently
        with the metrics themselves being incremented elsewhere -- reading a
        prometheus_client metric's current value doesn't require any
        coordination with its own internal locking."""
        now = time.time()
        for metric_obj, _sample_name, kind, _label, _color in _TRACKED:
            for family in metric_obj.collect():
                for sample in family.samples:
                    if sample.name.endswith("_created"):
                        continue  # Counters auto-expose a creation-timestamp sample; not data
                    key: SeriesKey = (sample.name, tuple(sorted(sample.labels.items())))
                    if kind == "counter":
                        previous = self._last_raw.get(key)
                        self._last_raw[key] = sample.value
                        # previous is None only because this label combo's
                        # counter child didn't exist until its first .inc()
                        # (prometheus_client creates it lazily) -- these
                        # Counters always start at 0 at process boot, right
                        # alongside this sampler, so "no prior sample" means
                        # "started from 0 since we began watching", not "an
                        # unknown pre-existing total to hide." The first
                        # real burst for a backend that's never failed
                        # before -- exactly the case this dashboard exists
                        # to catch -- must show up as the spike it is, not
                        # get reported as zero.
                        value = (
                            sample.value if previous is None else max(0.0, sample.value - previous)
                        )
                    else:
                        value = sample.value
                    self._series_for(key).append((now, value))

    def render_panels(self) -> list[dict]:
        """Returns one group per backend_name, each holding its own row of
        panels -- [{"backend_name": ..., "panels": [{"title": ..., "svg":
        ...}, ...]}, ...] -- ready for METRICS_PANEL_TEMPLATE to render one
        row per backend rather than a single pool of cards that happens to
        wrap wherever the browser's width cuts it off (which, with more than
        one backend, mixed different backends' cards together on the same
        row). Backend names are discovered from whichever series are
        actually present (a backend that's never had a retry or
        pool-exhaustion event simply won't have those two series yet; its
        healthy-servers gauge is always present once health-checked once)."""
        backend_names = sorted(
            {
                dict(labels)["backend_name"]
                for (_name, labels) in self._series
                if "backend_name" in dict(labels)
            }
        )

        groups = []
        for backend_name in backend_names:
            panels = []
            for _metric_obj, sample_name, _kind, label, color in _TRACKED:
                key: SeriesKey = (sample_name, (("backend_name", backend_name),))
                points = list(self._series.get(key, ()))
                panels.append({"title": label, "svg": _render_sparkline(points, color=color)})
            groups.append({"backend_name": backend_name, "panels": panels})
        return groups

    async def start(self, on_sample: Callable[[], Awaitable[None]] | None = None):
        """Samples immediately, then on `interval`, forever. `on_sample` (if
        given) is awaited after each sample -- main.py wires this to
        modules.broadcast.broadcast_metrics_update so connected admin
        clients get pushed the new sparklines on the same cadence they're
        taken, without a second independent timer."""
        while True:
            try:
                self.sample_now()
                if on_sample is not None:
                    await on_sample()
            except Exception as exc:
                logger.error(f"Failed to sample/broadcast metrics history: {exc}")
            await asyncio.sleep(self.interval)
