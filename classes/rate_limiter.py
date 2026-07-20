# classes/rate_limiter.py
import asyncio
import logging
import os
import time

from metrics import RATE_LIMIT_REJECTIONS_TOTAL

logger = logging.getLogger(__name__)

DEFAULT_RATE_LIMIT_REQUESTS = int(os.environ.get("RATE_LIMIT_REQUESTS", 100))
DEFAULT_RATE_LIMIT_WINDOW = int(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", 60))


class RateLimiter:
    """
    Sliding-window per-IP rate limiter.

    Tracks request timestamps per IP within a rolling window and rejects
    requests once the limit is reached. Completely independent of routing
    policy and IPTracker (404 penalties).

    CRUD API mirrors AsyncRoundRobin conventions.
    """

    def __init__(
        self,
        limit: int = DEFAULT_RATE_LIMIT_REQUESTS,
        window: int = DEFAULT_RATE_LIMIT_WINDOW,
        sweep_every: int = 1024,
    ):
        self.limit = limit
        self.window = window
        # Idle-IP entries are swept every `sweep_every` hits so the map stays
        # bounded by recently active clients rather than growing forever.
        self.sweep_every = sweep_every

        # ip -> list of request timestamps
        self._hits: dict[str, list[float]] = {}
        self._hits_since_sweep = 0

        self._lock = asyncio.Lock()

    def _sweep_stale_unsafe(self, now: float):
        """Drop IPs with no timestamps in the window. Call under the lock."""
        stale = [
            ip
            for ip, timestamps in self._hits.items()
            if not timestamps or now - timestamps[-1] >= self.window
        ]
        for ip in stale:
            del self._hits[ip]
        if stale:
            logger.debug(f"RateLimiter swept {len(stale)} idle IP(s)")

    # ------------------------------------------------------------------ #
    #  CREATE / record                                                     #
    # ------------------------------------------------------------------ #

    async def hit(self, ip: str) -> tuple[bool, float]:
        """
        Record a request from an IP.

        Returns (allowed, retry_after_seconds). When the request is allowed,
        retry_after is 0.0. When rejected, retry_after is how long until the
        oldest request leaves the window and capacity frees up.
        """
        async with self._lock:
            now = time.monotonic()

            self._hits_since_sweep += 1
            if self._hits_since_sweep >= self.sweep_every:
                self._hits_since_sweep = 0
                self._sweep_stale_unsafe(now)

            # Prune timestamps outside the rolling window
            timestamps = [t for t in self._hits.get(ip, []) if now - t < self.window]

            if len(timestamps) >= self.limit:
                self._hits[ip] = timestamps
                retry_after = round(self.window - (now - timestamps[0]), 1)
                logger.debug(
                    f"Rate limit exceeded for {ip!r}: "
                    f"{len(timestamps)}/{self.limit} in {self.window}s window"
                )
                RATE_LIMIT_REJECTIONS_TOTAL.inc()
                return False, retry_after

            timestamps.append(now)
            self._hits[ip] = timestamps
            return True, 0.0

    # ------------------------------------------------------------------ #
    #  READ                                                                #
    # ------------------------------------------------------------------ #

    async def get_usage(self, ip: str) -> dict:
        """Get current window usage for an IP."""
        async with self._lock:
            now = time.monotonic()
            window_count = len([t for t in self._hits.get(ip, []) if now - t < self.window])
            return {
                "ip": ip,
                "window_count": window_count,
                "limit": self.limit,
                "window_seconds": self.window,
                "remaining": max(self.limit - window_count, 0),
            }

    async def get_all(self) -> dict:
        """Full snapshot of all tracked IPs — for management UI / reporting."""
        async with self._lock:
            now = time.monotonic()
            usage = {}
            for ip, timestamps in self._hits.items():
                window_count = len([t for t in timestamps if now - t < self.window])
                if window_count:
                    usage[ip] = window_count
            return {
                "config": {
                    "limit": self.limit,
                    "window_seconds": self.window,
                },
                "usage": usage,
            }

    # ------------------------------------------------------------------ #
    #  UPDATE                                                              #
    # ------------------------------------------------------------------ #

    async def update_config(self, limit: int = None, window: int = None):
        """Live-update rate limit config without restart."""
        async with self._lock:
            if limit is not None:
                self.limit = limit
            if window is not None:
                self.window = window
            logger.debug(f"RateLimiter config updated: limit={self.limit} window={self.window}")

    # ------------------------------------------------------------------ #
    #  DELETE                                                              #
    # ------------------------------------------------------------------ #

    async def reset(self, ip: str):
        """Clear the request window for a single IP."""
        async with self._lock:
            self._hits.pop(ip, None)
            logger.debug(f"Rate limit window cleared for {ip!r}")

    async def clear_all(self):
        """Wipe all state — useful for testing or emergency reset."""
        async with self._lock:
            self._hits.clear()
            logger.debug("RateLimiter fully cleared")
