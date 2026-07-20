# decorators/rate_limit.py
import functools
import inspect
import logging
import math

from quart import Response, current_app, request

from classes.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)


def rate_limit(func=None, *, limit: int = None, window: int = None):
    """
    Per-IP rate limiting decorator.

    Used bare, all routes share the app-wide limiter (app.rate_limiter):

        @app.route("/<path:path>")
        @rate_limit
        @apython_lb(backend_name="my_backend", route_policy="round_robin")
        def proxy(path): pass

    With arguments, the route gets its own dedicated limiter:

        @rate_limit(limit=10, window=60)
    """
    if func is None:
        return functools.partial(rate_limit, limit=limit, window=window)

    route_limiter = None
    if limit is not None or window is not None:
        limiter_kwargs = {}
        if limit is not None:
            limiter_kwargs["limit"] = limit
        if window is not None:
            limiter_kwargs["window"] = window
        route_limiter = RateLimiter(**limiter_kwargs)

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        limiter = route_limiter or current_app.rate_limiter
        client_ip = request.remote_addr

        allowed, retry_after = await limiter.hit(client_ip)
        if not allowed:
            logger.debug(f"Rate limited {client_ip!r}, retry after {retry_after}s")
            return Response(
                "Rate limit exceeded. Try again later.",
                status=429,
                headers={
                    "Retry-After": str(math.ceil(retry_after)),
                    "Connection": "close",
                },
            )

        result = func(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result

    return wrapper
