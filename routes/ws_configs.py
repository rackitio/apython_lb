import asyncio
import logging

from quart import Blueprint, current_app, render_template_string, websocket

from classes import AsyncDb
from decorators import ip_restriction
from modules import broadcast_config_update, connected_clients
from templates import ACTIVE_BACKENDS_TEMPLATE, CONFIG_LIST_TEMPLATE

logger = logging.getLogger(__name__)

db = AsyncDb()

ws_configs = Blueprint("ws_configs", __name__)


def parse_ws_message(data: str) -> tuple[str, int] | None:
    """
    Parse a client frame of the form "select:<id>" or "delete:<id>".
    Returns (action, config_id), or None for anything malformed.
    """
    action, _, raw_id = data.partition(":")
    if action in ("select", "delete") and raw_id.isdigit():
        return action, int(raw_id)
    return None


@ws_configs.websocket("/ws/configs")
@ip_restriction.internal_only
async def index():
    connected_clients.add(websocket._get_current_object())

    try:
        # Send full current state on connect/reconnect so the client is
        # immediately up to date regardless of when they (re)connected.
        configs = await db.get_all_configs()

        backends = []
        for name, rr in current_app.backend_manager._backends.items():
            items = await rr.get_all()
            for url in items:
                backends.append({"name": name, "url": url})

        config_html = await render_template_string(
            CONFIG_LIST_TEMPLATE, configs=configs, backends=backends
        )
        backends_html = await render_template_string(ACTIVE_BACKENDS_TEMPLATE, backends=backends)
        await websocket.send(config_html + backends_html)

        while True:
            # Client frames: "select:<id>" bumps LastSelected so that config
            # wins for its name ("last config in wins"); "delete:<id>" removes
            # the config. Malformed frames are ignored, not fatal.
            data = await websocket.receive()

            parsed = parse_ws_message(data)
            if parsed is None:
                logger.debug(f"Ignoring malformed ws frame: {data!r}")
                continue

            action, config_id = parsed
            if action == "select":
                await db.update_last_selected(config_id)
            else:
                await db.delete_config(config_id)
            await broadcast_config_update()

    except asyncio.CancelledError:
        pass
    finally:
        connected_clients.discard(websocket._get_current_object())
