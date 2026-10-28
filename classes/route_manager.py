import asyncio
import logging
import os
import re
from dataclasses import dataclass, field

import tomllib

from lb_settings import VALID_POLICIES

logger = logging.getLogger(__name__)

# Same cadence/knob BackendManager already polls its own (DB-backed) config
# source with -- one "how often do we notice config changed" setting for the
# whole app, not a second env var that means almost the same thing.
CONFIG_POLL_INTERVAL = int(os.environ.get("CONFIG_POLL_INTERVAL", 10))  # seconds
ROUTE_CONFIG_FILE = os.environ.get("ROUTE_CONFIG_FILE", "/app/routes.toml")

_VALID_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
_DEFAULT_METHODS = frozenset({"GET", "POST"})


@dataclass(frozen=True)
class RouteEntry:
    path: str  # always starts and ends with "/" -- e.g. "/", "/v1/", "/api/special/"
    backend_name: str
    route_policy: str
    url_match: str | None = None
    url_rewrite: str | None = None
    methods: frozenset[str] = field(default_factory=lambda: _DEFAULT_METHODS)


def _validate_entry(raw: dict) -> RouteEntry:
    path = raw.get("path")
    if not isinstance(path, str) or not path.startswith("/") or not path.endswith("/"):
        raise ValueError(
            f"Route path {path!r} must start and end with '/' (e.g. '/', '/v1/') "
            "so prefix matching can't accidentally match a longer path segment "
            "(e.g. '/v1' matching '/v123abc')"
        )

    backend_name = raw.get("backend_name")
    if not backend_name:
        raise ValueError(f"Route {path!r} is missing required 'backend_name'")

    route_policy = raw.get("route_policy")
    if route_policy not in VALID_POLICIES:
        raise ValueError(
            f"Route {path!r} has invalid route_policy {route_policy!r}. "
            f"Must be one of {sorted(VALID_POLICIES)}"
        )

    url_match = raw.get("url_match")
    if url_match is not None:
        try:
            re.compile(url_match)
        except re.error as e:
            raise ValueError(
                f"Route {path!r} has invalid url_match regex {url_match!r}: {e}"
            ) from e

    methods_raw = raw.get("methods")
    if methods_raw is None:
        methods = _DEFAULT_METHODS
    else:
        methods = frozenset(m.upper() for m in methods_raw)
        invalid = methods - _VALID_HTTP_METHODS
        if invalid:
            raise ValueError(
                f"Route {path!r} has invalid methods {sorted(invalid)}. "
                f"Must be a subset of {sorted(_VALID_HTTP_METHODS)}"
            )
        if not methods:
            raise ValueError(f"Route {path!r} has an empty 'methods' list")

    return RouteEntry(
        path=path,
        backend_name=backend_name,
        route_policy=route_policy,
        url_match=url_match,
        url_rewrite=raw.get("url_rewrite"),
        methods=methods,
    )


def _load_entries(file_path: str) -> list[RouteEntry]:
    with open(file_path, "rb") as f:
        data = tomllib.load(f)

    raw_routes = data.get("routes", [])
    if not raw_routes:
        raise ValueError(f"No [[routes]] entries found in {file_path!r}")

    entries = [_validate_entry(raw) for raw in raw_routes]

    duplicates = {e.path for e in entries if sum(1 for o in entries if o.path == e.path) > 1}
    if duplicates:
        raise ValueError(f"Duplicate route path(s) in {file_path!r}: {sorted(duplicates)}")

    # Longest path first so match() can do a simple linear scan and return on
    # the first hit -- the most specific prefix is always checked before any
    # less-specific one that would also match the same request path.
    entries.sort(key=lambda e: len(e.path), reverse=True)
    return entries


class RouteManager:
    """
    In-memory table of dynamic route mappings (path -> backend_name/
    route_policy/url_match/url_rewrite/methods), loaded from a TOML file and
    hot-reloaded whenever that file's mtime changes -- so a mounted Docker
    volume or Kubernetes ConfigMap edit takes effect without a pod restart.

    Mirrors BackendManager's own poll-and-reconcile shape (classes/
    backend_manager.py) so a future SQLite-backed source can be added
    alongside this one later without changing how callers use match().
    """

    def __init__(
        self,
        file_path: str = ROUTE_CONFIG_FILE,
        poll_interval: int = CONFIG_POLL_INTERVAL,
    ):
        self.file_path = file_path
        self.poll_interval = poll_interval
        self._entries: list[RouteEntry] = []
        self._mtime: float | None = None

    def load_now(self) -> None:
        """
        Load (or reload) entries from disk. Raises on any invalid entry or a
        missing/empty file -- fail loud rather than silently serve a stale or
        partial route table. Call this once, synchronously, during app
        startup (before the app accepts its first request) so there's no
        window where routes are configured but not yet loaded; start() below
        only handles reloading after that.
        """
        entries = _load_entries(self.file_path)
        self._entries = entries
        self._mtime = os.stat(self.file_path).st_mtime
        logger.debug(f"Loaded {len(entries)} route(s) from {self.file_path!r}")

    def match(self, request_path: str, method: str) -> tuple[RouteEntry | None, bool]:
        """
        Longest-prefix match against configured route paths (nginx
        location-block semantics). Entries are pre-sorted longest-path-first
        by load_now()/_load_entries(), so the first prefix hit is always the
        most specific one.

        A request path with no trailing slash (e.g. "/v1") is treated as
        matching the entry for "/v1/" too -- Werkzeug would otherwise 308
        -redirect that case when "/v1/" was its own literal route (today's
        static behavior); matching it directly here preserves that without
        the round trip now that routing goes through one generic catch-all.

        Returns:
            (None, False)   -- no configured route covers this path
            (entry, False)  -- path matched but `method` isn't in entry.methods
            (entry, True)   -- full match
        """
        method = method.upper()
        for entry in self._entries:
            if request_path.startswith(entry.path) or request_path == entry.path.rstrip("/"):
                return entry, method in entry.methods
        return None, False

    async def start(self):
        """Background loop: reload whenever the file's mtime changes. Does
        NOT perform the initial load -- call load_now() yourself first."""
        while True:
            await asyncio.sleep(self.poll_interval)
            try:
                mtime = os.stat(self.file_path).st_mtime
                if mtime != self._mtime:
                    logger.debug(f"{self.file_path!r} changed on disk, reloading routes")
                    self.load_now()
            except Exception as exc:
                logger.error(f"Failed to reload routes from {self.file_path!r}: {exc}")
