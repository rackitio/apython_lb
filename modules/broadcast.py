import logging

from quart import current_app, render_template_string

from classes import AsyncDb
from templates import ACTIVE_BACKENDS_TEMPLATE, CONFIG_LIST_TEMPLATE

logger = logging.getLogger(__name__)

db = AsyncDb()

# Single authoritative set — imported by both ws_configs and this module.
# Never re-assign this reference; only mutate it with .add() and .discard().
connected_clients: set = set()


async def broadcast_config_update():
    """Broadcast updated configs and active backends to all connected clients."""
    configs = await db.get_all_configs()

    backends = []
    backend_manager = current_app.backend_manager
    logger.debug(f"[broadcast] _backends keys: {list(backend_manager._backends.keys())}")

    for name, rr in backend_manager._backends.items():
        items = await rr.get_all()
        logger.debug(f"[broadcast] {name} -> {items}")
        for url in items:
            backends.append({"name": name, "url": url})

    logger.debug(f"[broadcast] final backends list: {backends}")
    logger.debug(f"[broadcast] connected clients: {len(connected_clients)}")

    config_html = await render_template_string(
        CONFIG_LIST_TEMPLATE, configs=configs, backends=backends
    )
    backends_html = await render_template_string(ACTIVE_BACKENDS_TEMPLATE, backends=backends)
    payload = config_html + backends_html

    disconnected = set()
    for client in connected_clients:
        try:
            await client.send(payload)
        except Exception:
            # Mark for removal — do not mutate the set during iteration
            disconnected.add(client)
            logger.debug(
                f"[broadcast] Removed stale client, {len(connected_clients) - len(disconnected)} remaining."
            )

    connected_clients.difference_update(disconnected)
