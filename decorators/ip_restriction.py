import base64
import hmac
import ipaddress
import logging
import os
from functools import wraps

from quart import Response, abort, request, websocket

logger = logging.getLogger(__name__)

ALLOWED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
]


def is_internal_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
        return any(addr in network for network in ALLOWED_NETWORKS)
    except ValueError:
        return False


def basic_auth_ok(auth_header: str | None) -> bool:
    """
    Check an Authorization header against MANAGE_BASIC_AUTH ("user:password").

    When MANAGE_BASIC_AUTH is unset, auth is not required and this always
    passes. Comparison is constant-time. Read at call time (not import time)
    so tests and live config changes behave predictably.
    """
    expected = os.environ.get("MANAGE_BASIC_AUTH")
    if not expected:
        return True
    if not auth_header or not auth_header.startswith("Basic "):
        return False
    try:
        supplied = base64.b64decode(auth_header[len("Basic ") :], validate=True).decode()
    except (ValueError, UnicodeDecodeError):
        return False
    return hmac.compare_digest(supplied, expected)


def internal_only(f):
    """
    Restrict a route (HTTP or WebSocket) to internal source IPs, plus HTTP
    Basic auth when MANAGE_BASIC_AUTH is set.

    The IP check alone is weak on networks with other tenants or when this
    app sits behind a proxy (remote_addr becomes the proxy's IP) — set
    MANAGE_BASIC_AUTH in any deployment where the internal network is not
    fully trusted.
    """

    @wraps(f)
    async def decorated(*args, **kwargs):
        try:
            # Try WebSocket context first
            client_ip = websocket.remote_addr
            auth_header = websocket.headers.get("Authorization")
            is_websocket = True
        except RuntimeError:
            # Fall back to HTTP request context
            client_ip = request.remote_addr
            auth_header = request.headers.get("Authorization")
            is_websocket = False

        if not is_internal_ip(client_ip):
            logger.debug(f"Denied request from non-internal IP: {client_ip}")
            abort(403)

        if not basic_auth_ok(auth_header):
            logger.debug(f"Denied request with missing/bad credentials from {client_ip}")
            if is_websocket:
                abort(401)
            return Response(
                "Unauthorized",
                status=401,
                headers={"WWW-Authenticate": 'Basic realm="manage"'},
            )

        return await f(*args, **kwargs)

    return decorated
