import asyncio
import hashlib
import logging
import os

from classes import AsyncRoundRobin

logger = logging.getLogger(__name__)

# Hard cap on tracked session pins. A client that refuses cookies mints a new
# session per request, so an unbounded map is a memory-exhaustion vector.
DEFAULT_MAX_SESSIONS = int(os.environ.get("STICKY_SESSION_MAX_ENTRIES", 10000))


def _mask_session(session_id) -> str:
    """
    Return a non-reversible, stable tag for a session ID so logs can correlate
    requests without exposing the session token itself (it's auth-equivalent).
    """
    if session_id is None:
        return "none"
    digest = hashlib.sha256(str(session_id).encode()).hexdigest()
    return f"sess-{digest[:8]}"


class StickyRoundRobin:
    """
    A round-robin implementation that supports sticky sessions.

    Each session ID is pinned to a specific item. If that item is deleted,
    the session falls back to standard round-robin on next access.
    """

    def __init__(self, items=None, max_sessions: int = DEFAULT_MAX_SESSIONS):
        self._rr = AsyncRoundRobin(items)
        self._session_map: dict[str, any] = {}
        self._max_sessions = max_sessions
        self._map_lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    async def _get_valid_item_for_session(self, session_id: str) -> any:
        """Return the pinned item for a session, or None if stale/missing."""
        pinned = self._session_map.get(session_id)
        if pinned is not None and pinned in await self._rr.get_all():
            return pinned
        return None

    # ------------------------------------------------------------------ #
    #  Public API  (mirrors AsyncRoundRobin)                              #
    # ------------------------------------------------------------------ #

    async def add(self, item):
        await self._rr.add(item)

    async def get_all(self):
        return await self._rr.get_all()

    async def get_next(self, session_id: str | None = None):
        """
        Return the next item.

        - With a session_id:  always return the same (sticky) item for that
          session.  The first call pins the session; subsequent calls honour
          the pin.  If the pinned item has been deleted the session is
          re-pinned to whatever round-robin returns next.

        - Without a session_id:  plain round-robin, identical to
          AsyncRoundRobin.get_next().
        """
        if session_id is None:
            return await self._rr.get_next()

        async with self._map_lock:
            pinned = await self._get_valid_item_for_session(session_id)
            if pinned is not None:
                logger.debug(f"Sticky hit  session={_mask_session(session_id)}  item={pinned!r}")
                return pinned

            # No valid pin — advance the round-robin and pin the result.
            item = await self._rr.get_next()
            if item is not None:
                # At capacity, evict the oldest pin (dict preserves insertion
                # order) so the map stays bounded.
                if len(self._session_map) >= self._max_sessions:
                    evicted = next(iter(self._session_map))
                    del self._session_map[evicted]
                    logger.debug(f"Sticky map full — evicted session={_mask_session(evicted)}")
                self._session_map[session_id] = item
                logger.debug(f"Sticky pin  session={_mask_session(session_id)}  item={item!r}")
            return item

    def get_current(self, session_id: str | None = None):
        """
        Return the current item without advancing the index.

        With a session_id, returns the pinned item (if still valid).
        """
        if session_id is None:
            return self._rr.get_current()

        pinned = self._session_map.get(session_id)
        if pinned is not None:
            logger.debug(
                f"Get current (sticky) session={_mask_session(session_id)}  item={pinned!r}"
            )
            return pinned

        return self._rr.get_current()

    async def delete(self, item):
        """
        Delete an item from the pool.

        Any sessions pinned to the deleted item are cleared; they will be
        re-pinned on their next get_next() call.
        """
        await self._rr.delete(item)

        async with self._map_lock:
            stale = [sid for sid, v in self._session_map.items() if v == item]
            for sid in stale:
                del self._session_map[sid]
                logger.debug(
                    f"Cleared stale pin  session={_mask_session(sid)}  deleted item={item!r}"
                )

    async def replace(self, items: list) -> None:
        """
        Replace the entire backend pool with a new list.

        Removes stale entries (clearing any pinned sessions for them) and adds
        any new entries, leaving unchanged entries and their session pins intact.
        """
        current = await self._rr.get_all()
        new_set = set(items)
        current_set = set(current)

        # Remove entries no longer in the healthy set — delete() handles session cleanup
        for item in current_set - new_set:
            await self.delete(item)
            logger.debug(f"[replace] Removed stale backend: {item}")

        # Add entries that are newly healthy
        for item in new_set - current_set:
            await self.add(item)
            logger.debug(f"[replace] Added new backend: {item}")

    # ------------------------------------------------------------------ #
    #  Session-specific helpers (bonus, not in AsyncRoundRobin)           #
    # ------------------------------------------------------------------ #

    async def release_session(self, session_id: str):
        """Unpin a session so it gets a fresh assignment next call."""
        async with self._map_lock:
            if session_id in self._session_map:
                del self._session_map[session_id]
                logger.debug(f"Released session pin  session={_mask_session(session_id)}")

    async def get_session_map(self) -> dict:
        """Return a snapshot of current session → item mappings."""
        async with self._map_lock:
            return dict(self._session_map)
