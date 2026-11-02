# Changelog

All notable changes to apython_lb are documented here, starting from this
file's creation. Format loosely follows [Keep a
Changelog](https://keepachangelog.com/en/1.0.0/). Releases are cut as
annotated git tags (see [Releases](README.md#releases) in the README) —
entries here aren't retroactively populated for `v0.1.0`–`v0.5.2`, which
predate this file; see `git log <tag>` or the repo's tag list for that
history.

## [Unreleased]

### ⚠️ Breaking

- `routes.toml`'s shipped `/v1/` entry's `url_match` changed from
  `^v1/(.+)$` to `^v1/?(.*)$`. A bare request to a configured path prefix
  (e.g. `/v1` or `/v1/`, with nothing after it) now rewrites to the
  backend's root instead of forwarding completely unrewritten. If you're
  running a `routes.toml` based on an earlier build with the old pattern,
  update it and verify the new behavior before rolling out.
  ([#9](https://github.com/rackitio/apython_lb/pull/9))

### Added

- **Dynamic route configuration** (`routes.toml`, `ROUTE_CONFIG_FILE`): path
  → backend/policy/rewrite/methods mappings, hot-reloaded on change (no
  restart), replacing routes hardcoded in `main.py`. One generic dispatcher
  (`@dynamic_route`) matches by longest path prefix (nginx
  `location`-block semantics); a per-route `methods` field returns `405` +
  `Allow` for a disallowed method instead of falling through. Static,
  code-defined routes (`apython_lb()`) still work and coexist with
  `routes.toml` in the same app — confirmed a more specific static route
  always wins over the generic dynamic catch-all (Werkzeug ranks by
  specificity, not registration order); the corresponding gotcha is that an
  overlapping `routes.toml` entry is silently shadowed if a static route
  also covers that path. The `docs/k8s/deploy/` example manifests now show
  (commented out, opt-in) how to mount a ConfigMap over the image's
  baked-in default `routes.toml`, matching the existing TLS-secret
  `subPath` mount pattern already used there.
  ([#9](https://github.com/rackitio/apython_lb/pull/9))
- **Live metrics dashboard** on `/v1/manage`: per-backend sparkline graphs
  sampled directly from the in-process Prometheus registry (`metrics.py`)
  on a fixed interval (`METRICS_HISTORY_INTERVAL`/`METRICS_HISTORY_POINTS`)
  — no external Prometheus/Grafana needed. Three metrics per backend, one
  chart each:
  - `backend_healthy_servers{backend_name}` — raw gauge value over time
  - `lb_retries_total{backend_name}` — delta since the previous sample (a
    per-interval rate, not the raw cumulative counter)
  - `lb_no_healthy_backends_total{backend_name}` — same delta treatment;
    the direct signal for "requests are 503ing because the pool emptied"

  Pushed to every connected admin session over the existing `/ws/configs`
  WebSocket on the sampler's own interval.
  ([#10](https://github.com/rackitio/apython_lb/pull/10))

### Fixed

- `routes.toml`'s own inline comment incorrectly claimed `url_match` runs
  against the path *after* the `path` prefix is stripped — it never did.
  Corrected to describe the real contract: the full forwarded path, no
  leading slash, not pre-stripped of whatever `path` prefix matched the
  route. ([#9](https://github.com/rackitio/apython_lb/pull/9))
- The metrics dashboard's per-backend Counters (retries, pool-exhausted
  events) reported a delta of `0` on a series' first-ever observation
  instead of the real value — a backend's first failure, exactly the case
  the dashboard exists to catch, was invisible on the chart. Caught by
  replaying a real incident end-to-end against a live container before
  shipping, not just in unit tests.
  ([#10](https://github.com/rackitio/apython_lb/pull/10))

## [v0.5.2] and earlier

Not tracked in this file. See `git log v0.5.2` (or any earlier tag) and the
repository's tag list for history up to this point.
