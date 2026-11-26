import asyncio
import logging
import os
import re
from dataclasses import dataclass, field

from lb_settings import VALID_POLICIES
from modules.broadcast import broadcast_route_update

from .config_file import ConfigFile

logger = logging.getLogger(__name__)

# Same cadence/knob BackendManager already polls its own (DB-backed) config
# source with -- one "how often do we notice config changed" setting for the
# whole app, not a second env var that means almost the same thing.
CONFIG_POLL_INTERVAL = int(os.environ.get("CONFIG_POLL_INTERVAL", 10))  # seconds

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
    # Provenance only -- match() doesn't care which source an entry came from.
    # "file" entries are read-only from the API/UI's perspective (nothing
    # ever writes back to routes.toml); "db" entries are the ones the REST
    # API and admin UI can create/delete. db_id is the Routes table row id,
    # set only for source="db".
    source: str = "file"
    db_id: int | None = None


def _validate_entry(raw: dict, source: str = "file", db_id: int | None = None) -> RouteEntry:
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
        source=source,
        db_id=db_id,
    )


def _entries_from_raw(raw_routes: list[dict], source: str) -> list[RouteEntry]:
    """
    Validate the raw [[routes]] tables ConfigFile parsed out of the config
    file. A file with no [[routes]] at all is legitimate now that routes
    can also come from the REST API, so an empty list is returned rather
    than raised on -- ConfigFile itself refuses a file with no recognised
    sections at all.
    """
    entries = [_validate_entry(raw) for raw in raw_routes]

    duplicates = {e.path for e in entries if sum(1 for o in entries if o.path == e.path) > 1}
    if duplicates:
        raise ValueError(f"Duplicate route path(s) in {source!r}: {sorted(duplicates)}")

    # Longest path first so match() can do a simple linear scan and return on
    # the first hit -- the most specific prefix is always checked before any
    # less-specific one that would also match the same request path.
    entries.sort(key=lambda e: len(e.path), reverse=True)
    return entries


class RouteManager:
    """
    In-memory table of dynamic route mappings (path -> backend_name/
    route_policy/url_match/url_rewrite/methods), merged from two sources:

    - The [[routes]] section of apython_lb.toml, read through a shared
      ConfigFile (classes/config_file.py) and hot-reloaded whenever that
      file changes -- a mounted Docker volume or Kubernetes ConfigMap edit
      takes effect without a pod restart. Read-only from this class's own
      perspective: nothing here ever writes back to the file.
    - A `db` (AsyncDb), if one is given -- routes created/removed live via
      the `/v1/manage/routes` REST API, persisted in SQLite the same way
      backend configs already are. Polled on the same interval as the file.

    A path may only be defined by one source at a time -- see
    validate_new_path() (checked by the REST layer before insert) and the
    collision check in load_now() (checked when the file is reloaded,
    in case it's since been edited to claim a path an API-created route
    already owns).
    """

    def __init__(
        self,
        config_file=None,
        poll_interval: int = CONFIG_POLL_INTERVAL,
        db=None,
    ):
        self.config_file = config_file if config_file is not None else ConfigFile()
        self.poll_interval = poll_interval
        self.db = db
        self._file_entries: list[RouteEntry] = []
        self._db_entries: list[RouteEntry] = []
        self._entries: list[RouteEntry] = []
        self._file_generation: int | None = None
        self._db_rows_seen: list[dict] | None = None

    def _rebuild(self) -> None:
        """Merge file + DB entries into the single table match() reads from,
        longest-path first (unchanged ordering rule from Phase 1)."""
        merged = list(self._file_entries) + list(self._db_entries)
        merged.sort(key=lambda e: len(e.path), reverse=True)
        self._entries = merged

    def load_now(self) -> None:
        """
        Load (or reload) file entries. Raises on any invalid entry, a
        missing/malformed file, or a path that collides with an
        already-loaded DB-sourced route -- fail loud rather than silently
        serve a stale or partial route table. Call this once, synchronously,
        during app startup (before the app accepts its first request) so
        there's no window where routes are configured but not yet loaded;
        start() below only handles reloading after that.
        """
        generation = self.config_file.refresh()
        source = self.config_file.source_path
        entries = _entries_from_raw(self.config_file.routes, source)

        collisions = {e.path for e in entries} & {e.path for e in self._db_entries}
        if collisions:
            raise ValueError(
                f"{source!r} path(s) {sorted(collisions)} collide with an "
                "existing API-created route -- remove the API-created route first, "
                "or use a different path in the config file"
            )

        self._file_entries = entries
        self._file_generation = generation
        self._rebuild()
        logger.debug(f"Loaded {len(entries)} route(s) from {source!r}")

    async def reload_db(self) -> bool:
        """
        Fetch the current DB-sourced routes and merge them in. Returns True
        if anything changed (a route was added/removed since the last call),
        so callers know whether to broadcast. A no-op if this manager wasn't
        given a `db`. Call once at startup (awaited, after load_now()) and
        thereafter from the poll loop in start().

        A DB row that fails validation, or collides with a file-sourced
        path, is logged and skipped -- same "keep serving what's still
        valid" behavior load_now() already has for a bad file edit. Neither
        should happen in practice since the REST layer checks both before
        insert; this is a defensive backstop, not the primary guard.
        """
        if self.db is None:
            return False

        rows = await self.db.get_all_routes()
        if rows == self._db_rows_seen:
            return False

        file_paths = {e.path for e in self._file_entries}
        entries = []
        for row in rows:
            try:
                entry = _validate_entry(row, source="db", db_id=row["id"])
            except ValueError as exc:
                logger.error(f"Skipping invalid DB route {row.get('path')!r}: {exc}")
                continue
            if entry.path in file_paths:
                logger.error(f"Skipping DB route {entry.path!r}: collides with a routes.toml entry")
                continue
            entries.append(entry)

        self._db_entries = entries
        self._db_rows_seen = rows
        self._rebuild()
        return True

    def validate_entry(self, raw: dict) -> RouteEntry:
        """Validate a dict of route fields (same shape as a routes.toml
        entry or a DB row) without adding it anywhere -- used by the REST
        endpoint to check a submitted route before inserting it."""
        return _validate_entry(raw)

    def validate_new_path(self, path: str) -> None:
        """Raise ValueError if `path` already exists in the merged table
        (file- or DB-sourced). Called by the REST endpoint before insert --
        this is the actual collision guard; the Routes table's UNIQUE
        constraint on Path is defense-in-depth for a concurrent-insert race,
        not the primary check (it wouldn't catch a collision with a
        file-sourced path at all)."""
        for entry in self._entries:
            if entry.path == path:
                raise ValueError(
                    f"Route path {path!r} already exists (source: {entry.source}) -- "
                    "pick a different path, or remove the existing route first"
                )

    def list_entries(self) -> list[RouteEntry]:
        """All currently loaded routes, file- and DB-sourced, longest-path
        first -- for the admin UI/REST layer to render. match() reads
        self._entries directly; this is just a read-only view of the same
        list for callers outside this class."""
        return list(self._entries)

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
        """Background loop: reload whenever the file's mtime changes or the
        DB-sourced routes change, broadcasting to the admin UI on either.
        Does NOT perform the initial load -- call load_now() (and, if a db
        was given, await reload_db() once) yourself first."""
        while True:
            await asyncio.sleep(self.poll_interval)
            changed = False

            try:
                # refresh() reparses only when the file actually changed and
                # bumps its generation; comparing generations (rather than
                # mtimes) means BackendManager polling the same ConfigFile
                # can't consume a reload this manager then misses.
                if self.config_file.refresh() != self._file_generation:
                    logger.debug(
                        f"{self.config_file.source_path!r} changed on disk, reloading routes"
                    )
                    self.load_now()
                    changed = True
            except Exception as exc:
                logger.error(
                    f"Failed to reload routes from {self.config_file.source_path!r}: {exc}"
                )

            try:
                if await self.reload_db():
                    changed = True
            except Exception as exc:
                logger.error(f"Failed to reload DB-sourced routes: {exc}")

            if changed:
                await broadcast_route_update()
