import functools
import json
import logging
import re
import uuid

import httpx
from httpx import ConnectError, HTTPStatusError, RequestError, TimeoutException
from quart import Response, current_app, request, session

from lb_settings import (
    LB_MAX_ATTEMPTS,
    LB_UPSTREAM_HTTP2,
    LB_UPSTREAM_TIMEOUT_SECONDS,
    LB_UPSTREAM_VERIFY_TLS,
)
from metrics import (
    LB_BACKEND_ERRORS_TOTAL,
    LB_BACKEND_SELECTIONS_TOTAL,
    LB_NO_HEALTHY_BACKENDS_TOTAL,
    LB_RETRIES_TOTAL,
)

logger = logging.getLogger(__name__)

VALID_POLICIES = {"round_robin", "sticky_session", "sticky_ip"}
_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

# RFC 9110 hop-by-hop headers — they describe the connection between two hops
# and must not be forwarded by a proxy in either direction.
_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
# Request side: httpx computes Content-Length from the body it sends.
_REQUEST_EXCLUDED_HEADERS = _HOP_BY_HOP_HEADERS | {"content-length"}
# Response side: httpx transparently decompresses bodies, so the upstream
# Content-Encoding/Content-Length no longer describe the bytes we forward.
_RESPONSE_EXCLUDED_HEADERS = _HOP_BY_HOP_HEADERS | {"content-length", "content-encoding"}

# RFC 9110 idempotent methods — safe to replay after an ambiguous failure.
_IDEMPOTENT_METHODS = {"GET", "HEAD", "OPTIONS", "PUT", "DELETE", "TRACE"}


def _strip_headers(headers: dict, excluded: set) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in excluded}


def _safe_to_retry(method: str, exc: Exception) -> bool:
    """
    Idempotent methods can always be retried. Non-idempotent methods (POST,
    PATCH) are only retried when the failure happened before the request was
    sent (connect phase), so the backend cannot have processed it.
    """
    if method.upper() in _IDEMPOTENT_METHODS:
        return True
    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))


async def _check_and_record_404(ip: str, response_status: int) -> Response | None:
    """
    Records a 404 hit for an IP and returns a 429 Response if the threshold
    is crossed, otherwise returns None.
    """
    if response_status != 404:
        return None

    penalised = await current_app.ip_tracker.record_404(ip)
    if penalised:
        logger.debug(f"IP {ip!r} crossed 404 threshold, penalising")
        return Response(
            "Too many errors. Try again later.",
            status=429,
            headers={"Retry-After": "300", "Connection": "close"},
        )
    return None


def _apply_rewrite(path: str, url_match: str | None, url_rewrite: str | None) -> str:
    """
    Apply nginx-style regex rewrite to path.

    Supports:
      - Named groups:    (?P<name>...) referenced as \\g<name>
      - Numbered groups: (...)         referenced as \\1, \\2, etc.
      - Literal strings in url_rewrite are passed through unchanged.

    Returns the rewritten path, or the original path if no match/no rules.
    """
    if not url_match or url_rewrite is None:
        return path

    match = re.search(url_match, path)
    if not match:
        logger.debug(f"url_match {url_match!r} did not match path {path!r}, passing through")
        return path

    rewritten = match.expand(url_rewrite)
    logger.debug(
        f"Rewrote path {path!r} -> {rewritten!r}  (match={url_match!r}, rewrite={url_rewrite!r})"
    )
    return rewritten


async def _resolve_backend(backend: str, dns_resolver) -> str:
    """
    If the backend URL contains a hostname instead of a raw IP, resolve it.
    Returns the same URL with the hostname replaced by a resolved IP,
    or the original URL if it's already an IP or resolution fails.
    """
    try:
        scheme, host = backend.split("://", 1)
    except ValueError:
        return backend

    if _IP_RE.match(host):
        return backend  # already an IP, nothing to do

    ips = await dns_resolver.resolve(host)
    if not ips:
        logger.debug(f"Could not resolve {host!r} at request time, using as-is")
        return backend

    resolved = f"{scheme}://{ips[0]}"
    logger.debug(f"Resolved backend {backend!r} → {resolved!r}")
    return resolved


def apython_lb(
    func=None,
    *,
    backend_name: str = None,
    route_policy: str = None,
    url_match: str = None,
    url_rewrite: str = None,
):
    if func is None:
        return functools.partial(
            apython_lb,
            backend_name=backend_name,
            route_policy=route_policy,
            url_match=url_match,
            url_rewrite=url_rewrite,
        )

    if route_policy not in VALID_POLICIES:
        raise ValueError(f"Invalid route_policy '{route_policy}'. Must be one of {VALID_POLICIES}")

    # Validate the regex at decoration time — fail fast rather than at request time
    if url_match is not None:
        try:
            re.compile(url_match)
        except re.error as e:
            raise ValueError(f"Invalid url_match regex {url_match!r}: {e}") from e

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        raw_path = kwargs.get("path", "")

        # ── URL rewrite (nginx-style) — applied before any routing ───── #
        path = _apply_rewrite(raw_path, url_match, url_rewrite)

        attempt = 0
        session_id = None
        sticky_ip = current_app.backend_manager.get_sticky_ip()

        client_ip = request.remote_addr
        logger.debug(f"Request received from {client_ip}")

        # ── Early penalty check — before hitting any backend ─────────── #
        if await current_app.ip_tracker.is_penalised(client_ip):
            logger.debug(f"Blocked penalised IP {client_ip!r}")
            return Response(
                "Too many errors. Try again later.",
                status=429,
                headers={"Retry-After": "300", "Connection": "close"},
            )

        rr = current_app.backend_manager.get_healthy_backends(backend_name)
        if not rr:
            logger.debug(f"No backend pool found for '{backend_name}'")
            return Response(
                json.dumps({"error": f"No backend pool for '{backend_name}'"}),
                status=503,
                content_type="application/json",
            )

        if route_policy == "sticky_session":
            if "session_id" not in session:
                session["session_id"] = str(uuid.uuid4())
            session_id = session["session_id"]

        while attempt < LB_MAX_ATTEMPTS:
            # ── Routing — select a backend per policy on every attempt ──── #
            if route_policy == "sticky_ip":
                healthy_backend = await sticky_ip.get_next(client_ip, rr)
            else:  # sticky_session (session_id set) or round_robin (None)
                healthy_backend = await rr.get_next(session_id=session_id)
            logger.debug(f"Selected backend ({route_policy}): {healthy_backend}")

            if not healthy_backend:
                LB_NO_HEALTHY_BACKENDS_TOTAL.labels(backend_name=backend_name).inc()
                return Response(
                    json.dumps({"error": "No healthy backends available"}),
                    status=503,
                    content_type="application/json",
                )

            LB_BACKEND_SELECTIONS_TOTAL.labels(backend_name=backend_name, policy=route_policy).inc()

            # ── Resolve hostname → IP at request time (no-op for raw IPs) ── #
            resolved_backend = await _resolve_backend(
                healthy_backend, current_app.backend_manager.dns_resolver
            )

            url = f"{resolved_backend}/{path}"
            logger.debug(f"Forwarding request to: {url}")

            try:
                method = request.method
                body = await request.get_data()
                headers = _strip_headers(dict(request.headers), _REQUEST_EXCLUDED_HEADERS)
                headers["X-Forwarded-Proto"] = request.scheme
                headers["X-Forwarded-Host"] = request.host
                # Add X-Forwarded-For header
                existing_xff = headers.get("X-Forwarded-For")
                if existing_xff:
                    headers["X-Forwarded-For"] = f"{existing_xff}, {client_ip}"
                else:
                    headers["X-Forwarded-For"] = client_ip

                async with httpx.AsyncClient(
                    http2=LB_UPSTREAM_HTTP2, verify=LB_UPSTREAM_VERIFY_TLS
                ) as client:
                    try:
                        response = await client.request(
                            method,
                            url,
                            headers=headers,
                            content=body,
                            params=request.args,
                            timeout=LB_UPSTREAM_TIMEOUT_SECONDS,
                        )

                        # ── 404 tracking (all policies) ───────────────── #
                        penalty = await _check_and_record_404(client_ip, response.status_code)
                        if penalty:
                            return penalty

                        response.raise_for_status()

                        return Response(
                            response.content,
                            status=response.status_code,
                            headers=_strip_headers(
                                dict(response.headers), _RESPONSE_EXCLUDED_HEADERS
                            ),
                        )

                    except HTTPStatusError as e:
                        return Response(
                            e.response.content,
                            status=e.response.status_code,
                            headers=_strip_headers(
                                dict(e.response.headers), _RESPONSE_EXCLUDED_HEADERS
                            ),
                        )

                    except (TimeoutException, ConnectError) as e:
                        logger.debug(f"Connection error or timeout for {url}: {str(e)}")
                        await rr.delete(healthy_backend)
                        await sticky_ip.delete_backend(healthy_backend)
                        attempt += 1
                        LB_RETRIES_TOTAL.labels(backend_name=backend_name).inc()
                        if attempt >= LB_MAX_ATTEMPTS or not _safe_to_retry(method, e):
                            LB_BACKEND_ERRORS_TOTAL.labels(
                                backend_name=backend_name, error_type="timeout"
                            ).inc()
                            return Response(
                                json.dumps({"error": "All backend requests failed"}),
                                status=504,
                                content_type="application/json",
                            )
                        continue

            except RequestError as e:
                logger.debug(f"Error making request to {url}: {e}")
                await rr.delete(healthy_backend)
                await sticky_ip.delete_backend(healthy_backend)
                attempt += 1
                LB_RETRIES_TOTAL.labels(backend_name=backend_name).inc()
                if attempt >= LB_MAX_ATTEMPTS or not _safe_to_retry(request.method, e):
                    LB_BACKEND_ERRORS_TOTAL.labels(
                        backend_name=backend_name, error_type="request_error"
                    ).inc()
                    return Response(
                        json.dumps({"error": "All backend requests failed"}),
                        status=502,
                        content_type="application/json",
                    )
                continue

            except Exception as e:
                logger.debug(f"Unexpected error in load_balance: {e}")
                LB_BACKEND_ERRORS_TOTAL.labels(
                    backend_name=backend_name, error_type="unexpected"
                ).inc()
                return Response(
                    json.dumps({"error": "Internal server error"}),
                    status=500,
                    content_type="application/json",
                )

    return wrapper
