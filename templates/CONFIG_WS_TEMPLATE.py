CONFIG_WS_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <script src="https://cdn.jsdelivr.net/npm/htmx.org@2.0.8/dist/htmx.min.js" integrity="sha384-/TgkGk7p307TH7EXJDuUlgG3Ce1UVolAOFopFekQkkXihi5u/6OCvVKyz1W+idaz" crossorigin="anonymous"></script>
    <script src="https://cdn.jsdelivr.net/npm/htmx-ext-ws@2.0.4" integrity="sha384-1RwI/nvUSrMRuNj7hX1+27J8XDdCoSLf0EjEyF69nacuWyiJYoQ/j39RT1mSnd2G" crossorigin="anonymous"></script>
    <script src="https://cdn.jsdelivr.net/npm/htmx-ext-json-enc@2.0.1/json-enc.js"></script>
    <style>
        .config-item  { border: 1px solid #ccc; padding: 10px; margin: 10px 0; }
        .config-form  { border: 1px solid #aaa; padding: 15px; margin-bottom: 20px; max-width: 500px; }
        .config-form input { display: block; margin: 5px 0 10px; width: 100%; box-sizing: border-box; }
        .no-backends  { color: #999; font-style: italic; }
        .backend-health { margin-top: 6px; padding: 6px 10px; background: #f6f6f6; border-radius: 4px; font-size: 0.82rem; }
        .backend-health .healthy   { color: #2a9d2a; }
        .backend-health .unhealthy { color: #c0392b; }
        summary { display: flex; align-items: center; gap: 8px; cursor: pointer; }
        summary .summary-label { flex: 1; }
        .btn-remove { font-size: 0.75rem; padding: 2px 8px; cursor: pointer; color: red;
                      border: 1px solid red; background: none; border-radius: 4px; }
        .btn-select { font-size: 0.75rem; padding: 2px 8px; cursor: pointer; color: #2a9d2a;
                      border: 1px solid #2a9d2a; background: none; border-radius: 4px; }
        .rate-limit-config { margin: 4px 0 10px; }
        .rate-table { border-collapse: collapse; width: 100%; max-width: 600px; }
        .rate-table th, .rate-table td { padding: 4px 10px; text-align: left; border-bottom: 1px solid #eee; }
        .btn-refresh { font-size: 0.82rem; padding: 3px 10px; cursor: pointer; margin-bottom: 8px; }
    </style>
    <script>
        // Send a raw websocket message to the HTMX WS extension socket.
        // Called by the remove buttons so we stay on the single WS connection.
        function wsSend(msg) {
            const body = document.body;
            const sock = body._htmx_ws_connection;  // internal htmx-ext-ws handle
            if (sock && sock.readyState === WebSocket.OPEN) {
                sock.send(msg);
            }
        }
    </script>
</head>
<!-- ws-connect established on body so wsSend() can reach it via htmx internals -->
<body hx-ext="ws" ws-connect="/ws/configs">
    <h3>Add Config</h3>
    <div class="config-form">
        <form hx-ext="json-enc"
              hx-post="/v1/manage/configs"
              hx-target="#form-status"
              hx-swap="innerHTML"
              hx-on::after-request="this.reset()">
            <label>Name:    <input type="text" name="name"    placeholder="my-backend"              required></label>
            <label>Proto:   <input type="text" name="proto"   placeholder="https"                   required></label>
            <label>Host:    <input type="text" name="host"    placeholder="example.com"             required></label>
            <label>IPs:     <input type="text" name="ips"     placeholder="1.2.3.4,app.example.com" required></label>
            <label>HC Path: <input type="text" name="hc_path" placeholder="/healthz"                required></label>
            <button type="submit">Save</button>
        </form>
        <div id="form-status"></div>
    </div>

    <h3>Saved Configs</h3>
    <div id="config-list">
        {% for config in configs %}
        <details>
            <summary>
                <span class="summary-label">config id: {{ config.id }} — {{ config.name }}</span>
                <!-- onclick sends select:<id> / delete:<id> over the existing WS connection -->
                <button class="btn-select"
                        onclick="event.stopPropagation(); wsSend('select:{{ config.id }}')"
                        title="Make config {{ config.id }} the active one for its name">✓ select</button>
                <button class="btn-remove"
                        onclick="event.stopPropagation(); wsSend('delete:{{ config.id }}')"
                        title="Remove config {{ config.id }}">✕ remove</button>
            </summary>
            <div class="config-item">
                <p>
                    backend: {{ config.name }} proto: {{ config.data[0] }}<br>
                    Host: {{ config.data[1] }} IPs: {{ config.data[2] }} HC path: {{ config.data[3] }}<br>
                    Last selected: {{ config.last_selected }}
                </p>

                <!-- Backends grouped under their parent config -->
                {% set matched = backends | selectattr('config_id', 'equalto', config.id) | list %}
                {% if matched %}
                <div class="backend-health">
                    <strong>Active backends</strong>
                    {% for b in matched %}
                    <div>
                        <span class="{{ 'healthy' if b.healthy else 'unhealthy' }}">
                            {{ '✓' if b.healthy else '✗' }}
                        </span>
                        {{ b }}
                    </div>
                    {% endfor %}
                </div>
                {% else %}
                <p class="no-backends">No active backends for this config.</p>
                {% endif %}
            </div>
        </details>
        {% endfor %}
    </div>

    <h3>Rate Limited IPs</h3>
    <button class="btn-refresh"
            hx-get="/v1/manage/rate-limits"
            hx-target="#rate-limit-table"
            hx-swap="innerHTML">&#8635; Refresh</button>
    <div id="rate-limit-table">
        {{ rate_limit_html | safe }}
    </div>
</body>
</html>
"""
