METRICS_PANEL_TEMPLATE = """
<div id="metrics-panel" hx-swap-oob="true">
{% if panels %}
<div class="metrics-grid">
{% for panel in panels %}
    <div class="metrics-card">
        <div class="metrics-card-title">{{ panel.title }}</div>
        {{ panel.svg | safe }}
    </div>
{% endfor %}
</div>
{% else %}
<p class="no-backends">No backends configured yet -- metrics appear once one is added.</p>
{% endif %}
</div>
"""
