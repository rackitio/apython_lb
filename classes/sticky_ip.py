# classes/sticky_ip.py
import asyncio
import logging
import os

logger = logging.getLogger(__name__)


class StickyIP:
    """
    IP-based sticky routing. Maps client IPs to backend servers.
    Completely independent of IPTracker (penalty) and StickyRoundRobin (session).

    CRUD API mirrors AsyncRoundRobin conventions.
    """

    def __init__(self, max_entries: int = None):
        # ip -> backend
        self._ip_map: dict[str, str] = {}
        # Bounded so an attacker cycling many source IPs cannot grow the map
        # forever. Oldest pin is evicted first (dict preserves insertion order).
        self._max_entries = max_entries or int(os.environ.get("STICKY_IP_MAX_ENTRIES", 10000))
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    #  CREATE                                                              #
    # ------------------------------------------------------------------ #

    async def pin(self, ip: str, backend: str):
        """Pin a client IP to a backend."""
        async with self._lock:
            if ip not in self._ip_map and len(self._ip_map) >= self._max_entries:
                evicted = next(iter(self._ip_map))
                del self._ip_map[evicted]
                logger.debug(f"StickyIP map full — evicted {evicted!r}")
            self._ip_map[ip] = backend
            logger.debug(f"Pinned {ip!r} → {backend!r}")

    # ------------------------------------------------------------------ #
    #  READ                                                                #
    # ------------------------------------------------------------------ #

    async def get_next(self, ip: str, rr) -> str | None:
        """
        Main entry point — mirrors AsyncRoundRobin.get_next().

        Returns the pinned backend for this IP, or falls through to the
        provided StickyRoundRobin instance (rr) and pins the result.
        """
        async with self._lock:
            pinned = self._ip_map.get(ip)
            if pinned:
                logger.debug(f"IP sticky hit {ip!r} → {pinned!r}")
                return pinned

        # No pin yet — ask round robin (outside lock to avoid deadlock)
        backend = await rr.get_next(session_id=None)
        if backend:
            await self.pin(ip, backend)
        return backend

    async def get_pinned(self, ip: str) -> str | None:
        """Return the currently pinned backend for an IP, or None."""
        async with self._lock:
            return self._ip_map.get(ip)

    async def get_all(self) -> dict[str, str]:
        """Return a snapshot of all IP → backend mappings."""
        async with self._lock:
            return dict(self._ip_map)

    # ------------------------------------------------------------------ #
    #  DELETE                                                              #
    # ------------------------------------------------------------------ #

    async def release(self, ip: str):
        """Unpin a single IP — it will re-pin on next request."""
        async with self._lock:
            if ip in self._ip_map:
                del self._ip_map[ip]
                logger.debug(f"Released pin for {ip!r}")

    async def delete_backend(self, backend: str):
        """
        Remove all IPs pinned to a backend (call this when a backend goes down).
        Mirrors AsyncRoundRobin.delete() cascade behaviour.
        """
        async with self._lock:
            stale = [ip for ip, b in self._ip_map.items() if b == backend]
            for ip in stale:
                del self._ip_map[ip]
                logger.debug(f"Released {ip!r} — backend {backend!r} removed")
        return stale

    async def clear(self):
        """Wipe all pins."""
        async with self._lock:
            self._ip_map.clear()
            logger.debug("StickyIP fully cleared")
