import logging
import os

import tomllib

logger = logging.getLogger(__name__)

APYTHON_LB_CONFIG = os.environ.get("APYTHON_LB_CONFIG", "/app/apython_lb.toml")

# Deprecated, still honoured when apython_lb.toml is absent so existing
# deployments keep working across one release -- see _resolve_path().
LEGACY_ROUTE_CONFIG_FILE = os.environ.get("ROUTE_CONFIG_FILE", "/app/routes.toml")


class ConfigFile:
    """
    The single reader for apython_lb.toml -- the one file holding both
    [[routes]] and [[backends]].

    Both RouteManager and BackendManager consume this rather than opening
    the file themselves, so the file is stat'd and parsed once per change
    instead of once per manager, and both always see the same version of
    it (two independent readers can straddle a half-written file and end
    up disagreeing about what's configured).

    Consumers track changes by generation rather than by mtime: refresh()
    reloads when the file changed and bumps `generation`, so every
    consumer notices exactly one change exactly once, no matter which of
    them happened to trigger the reload.

    Parsing only -- no validation beyond "is this well-formed TOML with at
    least one section we recognise". Each manager validates its own
    section, so route rules stay in route_manager.py and backend rules in
    backend_manager.py.
    """

    def __init__(
        self,
        path: str = APYTHON_LB_CONFIG,
        legacy_path: str = LEGACY_ROUTE_CONFIG_FILE,
    ):
        self.path = path
        self.legacy_path = legacy_path
        self._routes: list[dict] = []
        self._backends: list[dict] = []
        self._mtime: float | None = None
        self._loaded_path: str | None = None
        self._generation = 0
        self._legacy_warned = False

    # ── what's actually on disk ──────────────────────────────────────── #

    def _resolve_path(self) -> str:
        """
        A legacy routes.toml wins whenever one exists, and this ordering is
        load-bearing rather than arbitrary.

        The image bakes in apython_lb.toml, so that file existing says
        nothing about operator intent -- it's just the shipped default. A
        routes.toml, by contrast, only exists because someone deliberately
        mounted one. Preferring the modern path would therefore mean an
        operator who mounts their existing routes.toml over the image
        silently gets the image's defaults instead of their own config,
        which is exactly the migration break the fallback exists to
        prevent. load() warns every time this path is taken.

        When neither exists, return the modern path so the error names the
        file operators should be creating.
        """
        if self.legacy_path != self.path and os.path.exists(self.legacy_path):
            return self.legacy_path
        return self.path

    @property
    def source_path(self) -> str:
        """Which file the currently loaded config came from -- shown in the
        admin UI so file-sourced entries say where they're defined."""
        return self._loaded_path or self._resolve_path()

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def routes(self) -> list[dict]:
        """Raw [[routes]] tables, exactly as parsed. Validated by RouteManager."""
        return self._routes

    @property
    def backends(self) -> list[dict]:
        """Raw [[backends]] tables, exactly as parsed. Validated by BackendManager."""
        return self._backends

    # ── loading ──────────────────────────────────────────────────────── #

    def _changed(self) -> bool:
        path = self._resolve_path()
        if path != self._loaded_path:
            return True
        try:
            return os.stat(path).st_mtime != self._mtime
        except OSError:
            # File vanished mid-run. Keep serving what we already parsed
            # rather than tearing the route table down.
            return False

    def load(self) -> None:
        """
        Parse the config file. Raises on a missing file or malformed TOML
        -- fail loud rather than silently serve an empty or partial table.
        """
        path = self._resolve_path()
        with open(path, "rb") as f:
            data = tomllib.load(f)

        routes = data.get("routes", [])
        backends = data.get("backends", [])

        is_legacy = path == self.legacy_path and path != self.path
        if is_legacy:
            if backends:
                raise ValueError(
                    f"{path!r} defines [[backends]], but it's being read as a legacy "
                    f"routes file because {self.path!r} doesn't exist. Rename it to "
                    f"{self.path!r} to use [[backends]]."
                )
            if not self._legacy_warned:
                logger.warning(
                    f"Reading routes from {path!r} (deprecated). Rename it to "
                    f"{self.path!r} -- a single file now holds both [[routes]] and "
                    "[[backends]], and ROUTE_CONFIG_FILE will stop being honoured "
                    "in a future release."
                )
                self._legacy_warned = True

        if not routes and not backends:
            raise ValueError(
                f"No [[routes]] or [[backends]] entries found in {path!r} -- "
                "an empty config is almost always a mistake, so this is refused "
                "rather than started with nothing configured"
            )

        self._routes = routes
        self._backends = backends
        self._mtime = os.stat(path).st_mtime
        self._loaded_path = path
        logger.debug(f"Loaded {len(routes)} route(s) and {len(backends)} backend(s) from {path!r}")

    def refresh(self) -> int:
        """
        Reload if the file changed on disk, and return the current
        generation. Consumers compare the returned value against the one
        they last acted on: equal means nothing to do, different means
        re-derive from .routes/.backends. Raises whatever load() raises, so
        a broken edit surfaces to the caller's own error handling.
        """
        if self._changed():
            self.load()
            self._generation += 1
        return self._generation
