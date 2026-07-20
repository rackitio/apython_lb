# classes/ip_tracker.py
import asyncio
import logging
import os
import time

from metrics import IP_TRACKER_404S_TOTAL, IP_TRACKER_PENALTIES_TOTAL

logger = logging.getLogger(__name__)

DEFAULT_404_THRESHOLD = int(os.environ.get("IP_TRACKER_404_THRESHOLD", 10))
DEFAULT_PENALTY_WINDOW = int(os.environ.get("IP_TRACKER_WINDOW_SECONDS", 60))
DEFAULT_PENALTY_DURATION = int(os.environ.get("IP_TRACKER_PENALTY_DURATION", 300))


class IPTracker:
    """
    Standalone penalty tracker for client IPs.

    Tracks 404s per IP within a rolling window and issues time-limited
    penalties when a threshold is exceeded. Completely independent of
    routing policy — usable with round_robin, sticky_session, sticky_ip.

    CRUD API mirrors AsyncRoundRobin conventions.
    """

    def __init__(
        self,
        threshold: int = DEFAULT_404_THRESHOLD,
        window: int = DEFAULT_PENALTY_WINDOW,
        duration: int = DEFAULT_PENALTY_DURATION,
        sweep_every: int = 1024,
    ):
        self.threshold = threshold
        self.window = window
        self.duration = duration
        # Idle entries are swept every `sweep_every` recorded 404s so the
        # maps stay bounded by recently active clients.
        self.sweep_every = sweep_every

        # ip -> list of 404 timestamps
        self._404_log: dict[str, list[float]] = {}
        # ip -> penalty expiry timestamp
        self._penalties: dict[str, float] = {}
        # ip -> total lifetime 404 count (for reporting; swept with the log)
        self._404_totals: dict[str, int] = {}
        self._404s_since_sweep = 0

        self._lock = asyncio.Lock()

    def _sweep_stale_unsafe(self, now: float):
        """
        Drop expired penalties and IPs with no 404s in the window (unless
        still penalised). Totals go with the log — they are reporting data,
        not worth unbounded memory. Call under the lock.
        """
        for ip in [ip for ip, exp in self._penalties.items() if exp <= now]:
            del self._penalties[ip]

        stale = [
            ip
            for ip, timestamps in self._404_log.items()
            if (not timestamps or now - timestamps[-1] >= self.window) and ip not in self._penalties
        ]
        for ip in stale:
            del self._404_log[ip]
            self._404_totals.pop(ip, None)
        if stale:
            logger.debug(f"IPTracker swept {len(stale)} idle IP(s)")

    # ------------------------------------------------------------------ #
    #  CREATE / record                                                     #
    # ------------------------------------------------------------------ #

    async def record_404(self, ip: str) -> bool:
        """
        Record a 404 hit for an IP.
        Returns True if the IP has just been penalised (threshold crossed).
        """
        async with self._lock:
            now = time.monotonic()

            self._404s_since_sweep += 1
            if self._404s_since_sweep >= self.sweep_every:
                self._404s_since_sweep = 0
                self._sweep_stale_unsafe(now)

            # Increment lifetime total
            self._404_totals[ip] = self._404_totals.get(ip, 0) + 1
            IP_TRACKER_404S_TOTAL.inc()

            # Prune timestamps outside the rolling window
            timestamps = self._404_log.get(ip, [])
            timestamps = [t for t in timestamps if now - t < self.window]
            timestamps.append(now)
            self._404_log[ip] = timestamps

            count = len(timestamps)
            logger.debug(f"404 recorded for {ip!r}: {count}/{self.threshold} in window")

            if count >= self.threshold and ip not in self._penalties:
                self._penalties[ip] = now + self.duration
                logger.debug(
                    f"IP {ip!r} penalised for {self.duration}s "
                    f"({count} 404s in {self.window}s window)"
                )
                IP_TRACKER_PENALTIES_TOTAL.inc()
                return True

            return False

    async def add_penalty(self, ip: str, duration: int = None):
        """Manually apply a penalty to an IP (e.g. from admin UI)."""
        async with self._lock:
            expiry = time.monotonic() + (duration or self.duration)
            self._penalties[ip] = expiry
            logger.debug(
                f"Manual penalty applied to {ip!r}, expires in {duration or self.duration}s"
            )

    # ------------------------------------------------------------------ #
    #  READ                                                                #
    # ------------------------------------------------------------------ #

    async def is_penalised(self, ip: str) -> bool:
        """Check if an IP is currently under penalty."""
        async with self._lock:
            return self._is_penalised_unsafe(ip)

    async def get_penalty(self, ip: str) -> dict | None:
        """
        Get penalty details for a specific IP.
        Returns None if not penalised.
        """
        async with self._lock:
            if not self._is_penalised_unsafe(ip):
                return None
            remaining = round(self._penalties[ip] - time.monotonic(), 1)
            return {
                "ip": ip,
                "remaining_seconds": remaining,
                "expires_at": self._penalties[ip],
            }

    async def get_404_count(self, ip: str) -> dict:
        """Get current window and lifetime 404 counts for an IP."""
        async with self._lock:
            now = time.monotonic()
            window_timestamps = self._404_log.get(ip, [])
            window_count = len([t for t in window_timestamps if now - t < self.window])
            return {
                "ip": ip,
                "window_count": window_count,
                "lifetime_count": self._404_totals.get(ip, 0),
                "threshold": self.threshold,
                "window_seconds": self.window,
            }

    async def get_all(self) -> dict:
        """
        Full snapshot of all tracked IPs — for management UI / reporting.
        Returns penalised IPs, 404 counts, and config.
        """
        async with self._lock:
            now = time.monotonic()

            penalised = {
                ip: round(exp - now, 1) for ip, exp in self._penalties.items() if exp > now
            }

            counts = {
                ip: {
                    "window_count": len([t for t in ts if now - t < self.window]),
                    "lifetime_count": self._404_totals.get(ip, 0),
                }
                for ip, ts in self._404_log.items()
            }

            return {
                "config": {
                    "threshold": self.threshold,
                    "window_seconds": self.window,
                    "penalty_duration_seconds": self.duration,
                },
                "penalised": penalised,
                "404_counts": counts,
            }

    async def get_all_penalised(self) -> list[dict]:
        """List all currently penalised IPs — convenience method for UI."""
        async with self._lock:
            now = time.monotonic()
            return [
                {
                    "ip": ip,
                    "remaining_seconds": round(exp - now, 1),
                }
                for ip, exp in self._penalties.items()
                if exp > now
            ]

    # ------------------------------------------------------------------ #
    #  UPDATE                                                              #
    # ------------------------------------------------------------------ #

    async def update_config(
        self,
        threshold: int = None,
        window: int = None,
        duration: int = None,
    ):
        """Live-update penalty config without restart."""
        async with self._lock:
            if threshold is not None:
                self.threshold = threshold
            if window is not None:
                self.window = window
            if duration is not None:
                self.duration = duration
            logger.debug(
                f"IPTracker config updated: threshold={self.threshold} "
                f"window={self.window} duration={self.duration}"
            )

    async def reset_404_count(self, ip: str):
        """Clear the 404 window for an IP without lifting a penalty."""
        async with self._lock:
            self._404_log.pop(ip, None)
            logger.debug(f"404 window cleared for {ip!r}")

    # ------------------------------------------------------------------ #
    #  DELETE                                                              #
    # ------------------------------------------------------------------ #

    async def delete(self, ip: str):
        """Fully remove an IP — clears penalty, 404 log, and totals."""
        async with self._lock:
            self._penalties.pop(ip, None)
            self._404_log.pop(ip, None)
            self._404_totals.pop(ip, None)
            logger.debug(f"IP {ip!r} fully removed from tracker")

    async def lift_penalty(self, ip: str):
        """Lift a penalty for an IP without clearing its 404 history."""
        async with self._lock:
            if ip in self._penalties:
                del self._penalties[ip]
                logger.debug(f"Penalty lifted for {ip!r}")

    async def clear_all(self):
        """Wipe all state — useful for testing or emergency reset."""
        async with self._lock:
            self._penalties.clear()
            self._404_log.clear()
            self._404_totals.clear()
            logger.debug("IPTracker fully cleared")

    # ------------------------------------------------------------------ #
    #  Internal                                                            #
    # ------------------------------------------------------------------ #

    def _is_penalised_unsafe(self, ip: str) -> bool:
        """Must be called within an existing lock."""
        expiry = self._penalties.get(ip)
        if expiry is None:
            return False
        if time.monotonic() < expiry:
            return True
        # Expired — clean up lazily
        del self._penalties[ip]
        return False
