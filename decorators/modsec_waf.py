import functools
import inspect
import logging

from quart import Response, current_app, request

logger = logging.getLogger(__name__)


def modsec_waf(func):
    """
    WAF decorator that inspects requests through the ModSecurity engine before
    forwarding to the load balancer.

    Place between @rate_limit and @apython_lb:

        @app.route("/<path:path>", methods=["GET", "POST"])
        @rate_limit
        @modsec_waf
        @apython_lb(backend_name="my_backend", route_policy="round_robin")
        def handler(path): pass

    Requires app.modsec to be a ModSecurityEngine instance.  Silently passes
    through if app.modsec is not configured.
    """

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        modsec = getattr(current_app, "modsec", None)

        if modsec is not None:
            client_ip = request.remote_addr
            method = request.method
            # Inspect the request URI (path + query), not the absolute URL:
            # REQUEST_URI-based rules (length limits, traversal patterns)
            # should not see the scheme and host.
            query = request.query_string.decode()
            uri = request.path + (f"?{query}" if query else "")
            headers = dict(request.headers)
            body = await request.get_data()

            allowed, status_code = await modsec.inspect_request(
                client_ip, method, uri, headers, body
            )

            if not allowed:
                logger.debug(f"ModSecurity blocked {client_ip!r}: {method} {uri} -> {status_code}")
                return Response(
                    "Forbidden",
                    status=status_code,
                    headers={"Connection": "close"},
                )

        result = func(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result

    return wrapper
