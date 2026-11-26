import asyncio
import logging
import os
import re

import httpx

from lb_settings import LB_UPSTREAM_HTTP2, LB_UPSTREAM_VERIFY_TLS
from metrics import BACKEND_HEALTHY_SERVERS
from modules.broadcast import broadcast_config_update

from .dns_resolver import AsyncDNSResolver
from .sqlite_db import AsyncDb
from .sticky_ip import StickyIP
from .sticky_session import StickyRoundRobin

logger = logging.getLogger(__name__)

HEALTH_CHECK_INTERVAL = int(os.environ.get("HEALTH_CHECK_INTERVAL", 60))  # seconds
CONFIG_POLL_INTERVAL = int(os.environ.get("CONFIG_POLL_INTERVAL", 10))  # seconds

# ── /health readiness tunables ─────────────────────────────────────────── #
# Minimum number of configured backend pools that must each have at least one
# verified-healthy server before /health reports ready. Unset (None) means
# "all of them" — the strict default for rolling deploys. Set to a lower
# number (0 short-circuits entirely) if you have many backends and want
# traffic routed as soon as a subset comes up.
_min_backends_env = os.environ.get("HEALTH_READY_MIN_BACKENDS")
HEALTH_READY_MIN_BACKENDS = int(_min_backends_env) if _min_backends_env else None

# Comma-separated backend names that, if set, are the ONLY backends /health
# waits on — a short circuit for rollouts where one canary backend proves the
# new container is wired up correctly and the rest can catch up async.
HEALTH_READY_BACKENDS = frozenset(
    name.strip() for name in os.environ.get("HEALTH_READY_BACKENDS", "").split(",") if name.strip()
)


_VALID_PROTOCOLS = {"http", "https"}


def _validate_backend(raw: dict) -> tuple[str, tuple]:
    """
    Validate one [[backends]] table from the config file and convert it to
    the (protocol, host_name, ip_str, hc_path) tuple _configs already
    stores, so every consumer below (health checks, _parse_servers,
    readiness) works identically whether a backend came from the file or
    the database.

    `servers` is a native TOML array here but is joined into the
    comma-separated string the DB-backed path has always used -- one
    internal shape, two friendlier front doors.
    """
    name = raw.get("name")
    if not name or not isinstance(name, str):
        raise ValueError(f"Backend entry is missing a 'name': {raw!r}")

    proto = raw.get("proto")
    if proto not in _VALID_PROTOCOLS:
        raise ValueError(
            f"Backend {name!r} has invalid proto {proto!r}. Must be one of "
            f"{sorted(_VALID_PROTOCOLS)}"
        )

    host = raw.get("host")
    if not host or not isinstance(host, str):
        raise ValueError(
            f"Backend {name!r} is missing required 'host' (the Host header sent "
            "when health-checking its servers)"
        )

    servers = raw.get("servers")
    if not isinstance(servers, list) or not servers:
        raise ValueError(
            f"Backend {name!r} needs a non-empty 'servers' array of IPs or hostnames, "
            f'e.g. servers = ["1.2.3.4", "app.example.com"] -- got {servers!r}'
        )
    if not all(isinstance(s, str) and s.strip() for s in servers):
        raise ValueError(f"Backend {name!r} has a non-string or empty entry in 'servers'")

    # Reserved, same as the API's hc_path: accepted and stored, but health
    # checks always probe "/" (see the README's "Health checks" section).
    hc_path = raw.get("health_check_path", "/")

    return name, (proto, host, ",".join(s.strip() for s in servers), hc_path)


def _backends_from_raw(raw_backends: list[dict], source: str) -> dict[str, tuple]:
    configs = {}
    for raw in raw_backends:
        name, config = _validate_backend(raw)
        if name in configs:
            raise ValueError(f"Duplicate backend name {name!r} in {source!r}")
        configs[name] = config
    return configs


class BackendManager:
    def __init__(
        self,
        db: AsyncDb,
        health_check_interval: int = HEALTH_CHECK_INTERVAL,
        config_poll_interval: int = CONFIG_POLL_INTERVAL,
        ready_min_backends: int | None = HEALTH_READY_MIN_BACKENDS,
        ready_backends: frozenset[str] = HEALTH_READY_BACKENDS,
        config_file=None,
    ):
        self.db = db
        self.config_file = config_file
        self.health_check_interval = health_check_interval
        self.config_poll_interval = config_poll_interval
        self.ready_min_backends = ready_min_backends
        self.ready_backends = ready_backends

        # Health checks use the same TLS policy as proxied traffic — a
        # backend that fails verification should not be marked healthy.
        self.http_client = httpx.AsyncClient(http2=LB_UPSTREAM_HTTP2, verify=LB_UPSTREAM_VERIFY_TLS)
        nameservers = os.environ.get("DNS_NAMESERVERS", "8.8.8.8,1.1.1.1")
        parsed_nameservers = AsyncDNSResolver.resolve_nameserver_hosts(nameservers)
        self.dns_resolver = AsyncDNSResolver(nameservers=parsed_nameservers)

        # name -> StickyRoundRobin
        self._backends: dict[str, StickyRoundRobin] = {}
        self._sticky_ip: StickyIP = StickyIP()
        # name -> config tuple (protocol, host_name, ip_str, path), merged
        # from both sources below
        self._configs: dict[str, tuple] = {}
        # File-defined backends, kept separately so API-created ones can be
        # distinguished from them (the admin UI renders file-defined
        # backends read-only, and a name may only be claimed by one source)
        self._file_configs: dict[str, tuple] = {}
        self._file_generation: int | None = None

        # Last known state snapshots for change detection
        # name -> frozenset of healthy URLs
        self._last_healthy: dict[str, frozenset] = {}
        # frozenset of config names present in DB
        self._last_config_names: frozenset = frozenset()

    def get_healthy_backends(self, name: str) -> StickyRoundRobin | None:
        return self._backends.get(name)

    def readiness(self) -> tuple[bool, int, int]:
        """
        Compute /health readiness.

        Returns (ready, healthy_backend_count, configured_backend_count).

        - No backends configured yet (fresh install): ready — there's
          nothing to be unhealthy.
        - `ready_backends` set: ready once every named backend has at least
          one verified-healthy server, ignoring all others.
        - Otherwise: ready once at least `ready_min_backends` configured
          backends (default: all of them) each have at least one
          verified-healthy server.

        A backend only counts once the background health-check loop has
        actually probed it and found >=1 healthy server — being configured
        is not enough.
        """
        total = len(self._configs)
        if total == 0:
            return True, 0, 0

        healthy_names = {name for name, servers in self._last_healthy.items() if servers}

        if self.ready_backends:
            ready = self.ready_backends.issubset(healthy_names)
            return ready, len(healthy_names & self.ready_backends), len(self.ready_backends)

        # Clamp so a tunable larger than the configured backend count can't
        # make /health permanently unready.
        min_backends = (
            total if self.ready_min_backends is None else min(self.ready_min_backends, total)
        )
        return len(healthy_names) >= min_backends, len(healthy_names), total

    def get_sticky_ip(self) -> StickyIP:
        return self._sticky_ip

    async def _parse_servers(self, config: tuple) -> tuple[str, list[str]]:
        """
        Parse server entries from config. Entries may be raw IPv4 addresses
        (1.2.3.4) or hostnames (app.example.com), comma/space separated.
        Returns (host_name, [proto://entry, ...]) — no resolution here;
        health checks and the decorator handle resolution.
        """
        protocol, host_name, ip_str, path = config
        entries = [e.strip() for e in re.split(r"[,\s]+", ip_str) if e.strip()]
        host_list = [f"{protocol}://{entry}" for entry in entries]
        logger.debug(f"Host list for {host_name}: {host_list}")
        return host_name, host_list

    async def _check_server(self, server: str, host_name: str) -> bool:
        try:
            response = await self.http_client.get(server, headers={"Host": host_name})
            return response.status_code == 200
        except httpx.HTTPError as exc:
            logger.debug(f"Connection error for {server}: {exc}")
            return False

    def reload_file(self) -> bool:
        """
        Reload file-defined backends if the shared config file changed.
        Returns True when the file-sourced view actually changed.

        A name already claimed by an API-created backend is refused for the
        whole reload (the previously loaded file config keeps serving),
        matching RouteManager.load_now()'s collision behavior -- one name,
        one owner, with no silent precedence either way.
        """
        if self.config_file is None:
            return False

        generation = self.config_file.refresh()
        if generation == self._file_generation:
            return False

        source = self.config_file.source_path
        file_configs = _backends_from_raw(self.config_file.backends, source)

        db_owned = set(self._configs) - set(self._file_configs)
        collisions = set(file_configs) & db_owned
        if collisions:
            raise ValueError(
                f"{source!r} backend name(s) {sorted(collisions)} collide with an "
                "existing API-created backend -- remove the API-created one first, "
                "or use a different name in the config file"
            )

        self._file_configs = file_configs
        self._file_generation = generation
        logger.debug(f"Loaded {len(file_configs)} backend(s) from {source!r}")
        return True

    def validate_new_name(self, name: str) -> None:
        """Raise if `name` is already defined by either source. Called by the
        REST layer before insert, the same way RouteManager.validate_new_path()
        guards route creation."""
        if name in self._file_configs:
            raise ValueError(
                f"Backend name {name!r} is already defined in the config file -- "
                "pick a different name, or remove it from the file first"
            )
        if name in self._configs:
            raise ValueError(
                f"Backend name {name!r} already exists -- pick a different name, "
                "or remove the existing backend first"
            )

    def file_backend_names(self) -> frozenset[str]:
        """Names defined by the config file, for the admin UI to render those
        entries read-only (nothing ever writes back to the file)."""
        return frozenset(self._file_configs)

    def list_configs(self, db_configs: list[dict]) -> list[dict]:
        """
        Unified backend list for the admin UI: file-defined backends first
        (tagged source="file", no row id since they aren't in the database),
        then API-created ones. Keeps the shape the templates already expect
        -- id/name/data/last_selected -- and adds `source` so the UI knows
        which entries it may offer a remove button for.
        """
        items = [
            {
                "id": None,
                "name": name,
                "data": list(config),
                "last_selected": None,
                "source": "file",
            }
            for name, config in sorted(self._file_configs.items())
        ]
        items.extend(
            {**config, "source": "db"}
            for config in db_configs
            if config["name"] not in self._file_configs
        )
        return items

    async def _poll_configs_once(self):
        """Run a single config-poll pass: sync backends with the config file
        and the DB state."""
        try:
            self.reload_file()
        except Exception as exc:
            logger.error(f"Failed to reload backends from the config file: {exc}")

        db_configs = await self.db.get_all_configs()
        current_names: set[str] = set()
        config_changed = False

        # File-defined backends first so a DB row can never quietly take over
        # a name the file owns -- validate_new_name() rejects that at creation
        # time, and this is the backstop if one slipped in anyway.
        for name, new_config in self._file_configs.items():
            current_names.add(name)
            if name not in self._configs or self._configs[name] != new_config:
                logger.debug(f"New/updated file-defined config for '{name}'")
                self._configs[name] = new_config
                config_changed = True
                if name not in self._backends:
                    self._backends[name] = StickyRoundRobin([])

        # Add new or updated backends. get_all_configs() orders rows by
        # LastSelected DESC, so the first row seen for a name is the most
        # recently created/selected config — skipping later duplicates makes
        # the newest config win ("last config in wins").
        for config in db_configs:
            name = config["name"]
            if name in self._file_configs:
                logger.warning(
                    f"Ignoring API-created backend '{name}': that name is defined in "
                    "the config file, which owns it"
                )
                continue
            if name in current_names:
                logger.debug(f"Duplicate config name '{name}', keeping the most recent one")
                continue
            current_names.add(name)
            data = config["data"]  # [protocol, host_name, ip_str, path]
            new_config = tuple(data)

            if name not in self._configs or self._configs[name] != new_config:
                logger.debug(f"New/updated config detected for '{name}'")
                self._configs[name] = new_config
                config_changed = True
                if name not in self._backends:
                    self._backends[name] = StickyRoundRobin([])

        # Remove backends no longer defined by either source
        for name in list(self._backends.keys()):
            if name not in current_names:
                logger.debug(f"Config removed for '{name}', dropping backend.")
                del self._backends[name]
                del self._configs[name]
                try:
                    BACKEND_HEALTHY_SERVERS.remove(name)
                except KeyError:
                    pass
                config_changed = True

        if config_changed:
            logger.debug("[poll] Config changed, broadcasting.")
            self._last_config_names = frozenset(current_names)
            await broadcast_config_update()

    async def _poll_configs(self):
        """Continuously poll DB for config changes and add/remove backends."""
        while True:
            try:
                await self._poll_configs_once()
            except Exception as exc:
                logger.debug(f"Error polling configs: {exc}")

            await asyncio.sleep(self.config_poll_interval)

    async def _resolve_entry(self, entry: str, protocol: str) -> list[str]:
        """
        Given a proto://host_or_ip entry, return a list of proto://ip entries.
        Raw IPs are returned as-is (single item). Hostnames are resolved via DNS.
        """
        # Strip scheme for parsing
        host = entry.split("://", 1)[-1]
        ip_pattern = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
        if ip_pattern.match(host):
            return [entry]  # already an IP

        ips = await self.dns_resolver.resolve(host)
        if not ips:
            logger.debug(f"DNS resolution returned no results for {host!r}")
            return []
        return [f"{protocol}://{ip}" for ip in ips]

    async def _health_check_loop(self):
        """Continuously health-check all known backends."""
        while True:
            health_changed = False

            for name, config in list(self._configs.items()):
                try:
                    protocol, host_name, _, _ = config
                    host_name, all_entries = await self._parse_servers(config)

                    # Resolve hostnames → IPs before health-checking
                    all_servers: list[str] = []
                    for entry in all_entries:
                        all_servers.extend(await self._resolve_entry(entry, protocol))

                    healthy = [s for s in all_servers if await self._check_server(s, host_name)]
                    healthy_set = frozenset(healthy)
                    BACKEND_HEALTHY_SERVERS.labels(backend_name=name).set(len(healthy_set))

                    # Compare against last known healthy set for this config
                    if healthy_set != self._last_healthy.get(name):
                        logger.debug(
                            f"[health] '{name}' backends changed: "
                            f"{self._last_healthy.get(name)} -> {healthy_set}"
                        )
                        self._last_healthy[name] = healthy_set
                        health_changed = True

                    # Find servers that just went down and evict from StickyIP
                    current = await self._backends[name].get_all()
                    dead = [s for s in current if s not in healthy]
                    for backend in dead:
                        await self._sticky_ip.delete_backend(backend)

                    # Replace the full backend list with exactly the current healthy
                    # set rather than appending — add() alone never removes stale entries
                    await self._backends[name].replace(healthy)

                except Exception as exc:
                    logger.debug(f"Error health-checking '{name}': {exc}")

            if health_changed:
                logger.debug("[health] Backend state changed, broadcasting.")
                await broadcast_config_update()
            else:
                logger.debug("[health] No backend changes detected, skipping broadcast.")

            await asyncio.sleep(self.health_check_interval)

    async def start(self):
        """Start both background loops as concurrent tasks."""
        # Populate configs before the loops start racing each other.
        # Without this, _health_check_loop's first pass can see an empty
        # self._configs (its task gets scheduled before _poll_configs_once()
        # finishes its first DB read) and then sleep for a full
        # health_check_interval before checking anything — on a fresh
        # container with pre-existing DB configs that stalls /health
        # readiness for up to a minute for no reason.
        await self._poll_configs_once()
        await asyncio.gather(
            self._poll_configs(),
            self._health_check_loop(),
        )

    async def close(self):
        await self.http_client.aclose()
