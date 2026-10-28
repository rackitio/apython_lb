METRICS_PANEL_TEMPLATE = """
<div id="metrics-panel" hx-swap-oob="true">
{% if groups %}
{% for group in groups %}
<div class="metrics-backend-group">
    <div class="metrics-backend-name">{{ group.backend_name }}</div>
    <div class="metrics-grid">
    {% for panel in group.panels %}
        <div class="metrics-card">
            <div class="metrics-card-title">{{ panel.title }}</div>
            {{ panel.svg | safe }}
        </div>
    {% endfor %}
    </div>
</div>
{% endfor %}
{% else %}
<p class="no-backends">No backends configured yet -- metrics appear once one is added.</p>
{% endif %}
</div>
"""
