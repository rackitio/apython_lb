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


class BackendManager:
    def __init__(
        self,
        db: AsyncDb,
        health_check_interval: int = HEALTH_CHECK_INTERVAL,
        config_poll_interval: int = CONFIG_POLL_INTERVAL,
        ready_min_backends: int | None = HEALTH_READY_MIN_BACKENDS,
        ready_backends: frozenset[str] = HEALTH_READY_BACKENDS,
    ):
        self.db = db
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
        # name -> config tuple (protocol, host_name, ip_str, path)
        self._configs: dict[str, tuple] = {}

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

    async def _poll_configs_once(self):
        """Run a single config-poll pass: sync backends with the DB state."""
        db_configs = await self.db.get_all_configs()
        current_names: set[str] = set()
        config_changed = False

        # Add new or updated backends. get_all_configs() orders rows by
        # LastSelected DESC, so the first row seen for a name is the most
        # recently created/selected config — skipping later duplicates makes
        # the newest config win ("last config in wins").
        for config in db_configs:
            name = config["name"]
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

        # Remove backends no longer in DB
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
