ROUTE_LIST_TEMPLATE = """
<div id="route-list" hx-swap-oob="true">
{% for route in routes %}
<details>
    <summary>
        <span class="summary-label">{{ route.path }} &rarr; {{ route.backend_name }}</span>
        {% if route.source == "db" %}
        <form hx-delete="/v1/manage/routes/{{ route.db_id }}"
              hx-confirm="Remove route {{ route.path }}?"
              hx-target="closest details"
              hx-swap="outerHTML"
              style="margin:0">
            <button type="submit"
                    onclick="event.stopPropagation()"
                    class="btn-remove">&#10005; remove</button>
        </form>
        {% else %}
        <span class="route-source">from config file</span>
        {% endif %}
    </summary>
    <div class="config-item">
        <p>
            policy: {{ route.route_policy }}<br>
            methods: {{ route.methods | sort | join(', ') }}
            {% if route.url_match %}<br>url_match: <code>{{ route.url_match }}</code>{% endif %}
            {% if route.url_rewrite %}<br>url_rewrite: <code>{{ route.url_rewrite }}</code>{% endif %}
        </p>
    </div>
</details>
{% endfor %}
</div>
"""
