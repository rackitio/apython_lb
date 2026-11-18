import aiosqlite
from quart import Blueprint, current_app, render_template_string, request

from classes import AsyncDb
from decorators import ip_restriction
from templates import CONFIG_WS_TEMPLATE, METRICS_PANEL_TEMPLATE, ROUTE_LIST_TEMPLATE

db = AsyncDb()

v1_manage = Blueprint("v1_manage", __name__)

RATE_LIMIT_PARTIAL = """
<p class="rate-limit-config">
    Limit: <strong>{{ rate_limit_data.config.limit }}</strong> requests
    / <strong>{{ rate_limit_data.config.window_seconds }}s</strong> window
</p>
{% if rate_limit_data.usage %}
<table class="rate-table">
    <thead><tr><th>IP</th><th>Requests in window</th><th></th></tr></thead>
    <tbody>
    {% for ip, count in rate_limit_data.usage.items() %}
    <tr>
        <td>{{ ip }}</td>
        <td>{{ count }} / {{ rate_limit_data.config.limit }}</td>
        <td>
            <button class="btn-remove"
                    hx-delete="/v1/manage/rate-limits/{{ ip }}"
                    hx-target="#rate-limit-table"
                    hx-swap="innerHTML">reset</button>
        </td>
    </tr>
    {% endfor %}
    </tbody>
</table>
{% else %}
<p class="no-backends">No IPs currently tracked.</p>
{% endif %}
"""


@v1_manage.route("/v1/manage", methods=["GET"])
@ip_restriction.internal_only
async def index():
    configs = await db.get_all_configs()
    backends = []
    for rr in current_app.backend_manager._backends.values():
        items = await rr.get_all()
        backends.extend(items)
    rate_limit_data = await current_app.rate_limiter.get_all()
    rate_limit_html = await render_template_string(
        RATE_LIMIT_PARTIAL, rate_limit_data=rate_limit_data
    )
    metrics_html = await render_template_string(
        METRICS_PANEL_TEMPLATE, groups=current_app.metrics_history.render_panels()
    )
    route_html = await render_template_string(
        ROUTE_LIST_TEMPLATE, routes=current_app.route_manager.list_entries()
    )
    return await render_template_string(
        CONFIG_WS_TEMPLATE,
        configs=configs,
        backends=backends,
        rate_limit_html=rate_limit_html,
        metrics_html=metrics_html,
        route_html=route_html,
    )


@v1_manage.route("/v1/manage/rate-limits", methods=["GET"])
@ip_restriction.internal_only
async def get_rate_limits():
    rate_limit_data = await current_app.rate_limiter.get_all()
    return await render_template_string(RATE_LIMIT_PARTIAL, rate_limit_data=rate_limit_data)


@v1_manage.route("/v1/manage/rate-limits/<string:ip>", methods=["DELETE"])
@ip_restriction.internal_only
async def reset_rate_limit(ip):
    await current_app.rate_limiter.reset(ip)
    rate_limit_data = await current_app.rate_limiter.get_all()
    return await render_template_string(RATE_LIMIT_PARTIAL, rate_limit_data=rate_limit_data)


@v1_manage.route("/v1/manage/configs", methods=["POST"])
@ip_restriction.internal_only
async def create_config():
    data = await request.get_json()

    name = data.get("name")
    proto = data.get("proto")
    host = data.get("host")
    ips = data.get("ips")
    hc_path = data.get("hc_path")

    if not all([name, proto, host, ips, hc_path]):
        return '<span style="color:red">✗ Missing required fields</span>', 400

    await db.insert_config(name, [proto, host, ips, hc_path])

    # The WS broadcast will handle the update. The POST just needs to confirm success:
    # await broadcast_config_update()

    return '<span style="color:green">✓ Config saved</span>', 201


@v1_manage.route("/v1/manage/configs/<int:config_id>", methods=["DELETE"])
@ip_restriction.internal_only
async def delete_config(config_id):
    await db.delete_config(config_id)

    # Same reasoning — let the poll loop detect the removal and broadcast.
    # The hx-swap="none" on the button means HTMX ignores this response body anyway.
    # await broadcast_config_update()

    # HTMX out-of-band swap handles the list refresh via the broadcast;
    # return empty 200 so HTMX doesn't try to swap the trigger element
    return "", 200


@v1_manage.route("/v1/manage/routes", methods=["POST"])
@ip_restriction.internal_only
async def create_route():
    data = await request.get_json()

    path = data.get("path")
    backend_name = data.get("backend_name")
    route_policy = data.get("route_policy")
    url_match = data.get("url_match") or None
    url_rewrite = data.get("url_rewrite") or None
    methods_raw = data.get("methods")  # comma/space-separated string, optional

    if not all([path, backend_name, route_policy]):
        return '<span style="color:red">✗ Missing required fields</span>', 400

    methods = None
    if methods_raw:
        methods = [m for m in methods_raw.replace(",", " ").split() if m]

    route_manager = current_app.route_manager
    try:
        entry = route_manager.validate_entry(
            {
                "path": path,
                "backend_name": backend_name,
                "route_policy": route_policy,
                "url_match": url_match,
                "url_rewrite": url_rewrite,
                "methods": methods,
            }
        )
        route_manager.validate_new_path(entry.path)
    except ValueError as exc:
        return f'<span style="color:red">✗ {exc}</span>', 400

    try:
        await db.insert_route(
            entry.path,
            entry.backend_name,
            entry.route_policy,
            entry.url_match,
            entry.url_rewrite,
            sorted(entry.methods),
        )
    except aiosqlite.IntegrityError:
        # Only reachable via a race against another insert of the same path
        # between validate_new_path() above and this insert -- the Routes
        # table's UNIQUE constraint is the backstop for that window.
        return '<span style="color:red">✗ Route path already exists</span>', 400

    # The poll loop will pick up the new row and broadcast it, same as
    # create_config above:
    # await broadcast_route_update()

    return '<span style="color:green">✓ Route saved</span>', 201


@v1_manage.route("/v1/manage/routes/<int:route_id>", methods=["DELETE"])
@ip_restriction.internal_only
async def delete_route(route_id):
    await db.delete_route(route_id)

    # Same reasoning as delete_config above -- let the poll loop detect the
    # removal and broadcast.
    # await broadcast_route_update()

    return "", 200
