from quart import Blueprint, current_app, render_template_string, request

from classes import AsyncDb
from decorators import ip_restriction
from templates import CONFIG_WS_TEMPLATE

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
    return await render_template_string(
        CONFIG_WS_TEMPLATE, configs=configs, backends=backends, rate_limit_html=rate_limit_html
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
