ACTIVE_BACKENDS_TEMPLATE = """
<div id="active-backends" hx-swap-oob="true">
    {% if backends %}
        {% for backend in backends %}
        <details>
            <summary>{{ backend }}</summary>
            <div class="config-item">
                <p>{{ backend }}</p>
            </div>
        </details>
        {% endfor %}
    {% else %}
        <p class="no-backends">No active backends.</p>
    {% endif %}
</div>
"""
