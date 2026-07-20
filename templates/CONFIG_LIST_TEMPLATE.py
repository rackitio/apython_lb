CONFIG_LIST_TEMPLATE = """
<div id="config-list" hx-swap-oob="true">
{% for config in configs %}
<details>
    <summary style="display:flex; align-items:center; gap:8px;">
        <span style="flex:1">config id: {{ config.id }} — {{ config.name }}</span>
        <!-- wsSend() is defined on the parent page (CONFIG_WS_TEMPLATE) -->
        <button type="button"
                onclick="event.stopPropagation(); wsSend('select:{{ config.id }}')"
                title="Make config {{ config.id }} the active one for its name"
                style="font-size:0.75rem; padding:2px 8px; cursor:pointer;
                       color:#2a9d2a; border:1px solid #2a9d2a; background:none;
                       border-radius:4px;">✓ select</button>
        <form hx-delete="/v1/manage/configs/{{ config.id }}"
              hx-confirm="Remove config {{ config.id }}?"
              hx-target="closest details"
              hx-swap="outerHTML"
              style="margin:0">
            <button type="submit"
                    onclick="event.stopPropagation()"
                    style="font-size:0.75rem; padding:2px 8px; cursor:pointer;
                           color:red; border:1px solid red; background:none;
                           border-radius:4px;">✕ remove</button>
        </form>
    </summary>
    <div class="config-item">
        <p>
            backend: {{ config.name }} proto: {{ config.data[0] }}<br>
            Host: {{ config.data[1] }} IPs: {{ config.data[2] }} HC path: {{ config.data[3] }}<br>
            Last selected: {{ config.last_selected }}
        </p>

        {% set matched = backends | selectattr('name', 'equalto', config.name) | list %}
        {% if matched %}
        <div class="backend-health">
            <strong>Active backends</strong>
            {% for b in matched %}
            <div>✓ {{ b.url }}</div>
            {% endfor %}
        </div>
        {% else %}
        <p class="no-backends">No active backends for this config.</p>
        {% endif %}
    </div>
</details>
{% endfor %}
</div>
"""
