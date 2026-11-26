import logging

from quart import current_app, render_template_string

from classes import AsyncDb
from templates import (
    ACTIVE_BACKENDS_TEMPLATE,
    CONFIG_LIST_TEMPLATE,
    METRICS_PANEL_TEMPLATE,
    ROUTE_LIST_TEMPLATE,
)

logger = logging.getLogger(__name__)

db = AsyncDb()

# Single authoritative set — imported by both ws_configs and this module.
# Never re-assign this reference; only mutate it with .add() and .discard().
connected_clients: set = set()


async def broadcast_config_update():
    """Broadcast updated configs and active backends to all connected clients."""
    backend_manager = current_app.backend_manager
    configs = backend_manager.list_configs(await db.get_all_configs())

    backends = []
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


async def broadcast_route_update():
    """
    Broadcast the current merged route table (routes.toml + API-created
    routes) to every connected admin session. Called by RouteManager's own
    poll loop (classes/route_manager.py) when either source changes --
    mirrors broadcast_config_update()'s event-triggered shape above, just
    for routes instead of backend configs.
    """
    routes = current_app.route_manager.list_entries()
    payload = await render_template_string(ROUTE_LIST_TEMPLATE, routes=routes)

    disconnected = set()
    for client in connected_clients:
        try:
            await client.send(payload)
        except Exception:
            disconnected.add(client)

    connected_clients.difference_update(disconnected)


async def broadcast_metrics_update():
    """
    Broadcast the latest metrics sparklines to all connected management-UI
    clients. Called by MetricsHistory's own sampling loop (classes/
    metrics_history.py) on its sampling interval, independent of
    broadcast_config_update()'s event-triggered broadcasts above -- metrics
    change continuously, not just when a config/backend-health event occurs.
    """
    groups = current_app.metrics_history.render_panels()
    payload = await render_template_string(METRICS_PANEL_TEMPLATE, groups=groups)

    disconnected = set()
    for client in connected_clients:
        try:
            await client.send(payload)
        except Exception:
            disconnected.add(client)

    connected_clients.difference_update(disconnected)
