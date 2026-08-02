<!--
  This is the "Overview" text for the Docker Hub repository page, not a
  project README. Docker Hub renders it under Repositories → apython_lb →
  Overview. Update it in the Docker Hub UI (Manage Repository → Description)
  after edits, or wire it up via docker/hub-tools' sync-readme action if you
  add one to the release workflow.
-->

# apython_lb

An async HTTP(S) load balancer and reverse proxy built on
[Quart](https://quart.palletsprojects.com/). It fronts a pool of backends
with round-robin or sticky routing, health checks with a circuit breaker,
per-IP rate limiting, a libmodsecurity-powered WAF, URL rewriting, Prometheus
metrics, and a live management web UI.

**Source & full docs:** https://github.com/rackitio/apython_lb

## Supported tags

| Tag | Description |
|---|---|
| `latest`, `X.Y.Z`, `X.Y` | Built from the corresponding `vX.Y.Z` release tag in the source repo |

Images are multi-arch (`linux/amd64`, `linux/arm64`) and rebuilt only on
tagged releases — `latest` always tracks the newest release, never an
unreleased commit.

## Quick start

The container serves TLS directly and expects `/app/cert.pem` and
`/app/key.pem`. Generate a self-signed pair for local testing (bring a
CA-issued cert/key for anything real):

```bash
mkdir -p certs
openssl req -x509 -newkey rsa:2048 -keyout certs/key.pem -out certs/cert.pem \
  -days 365 -nodes -subj "/CN=localhost"

docker run --rm -p 8443:443 \
  -v "$PWD/certs/cert.pem":/app/cert.pem:ro \
  -v "$PWD/certs/key.pem":/app/key.pem:ro \
  <dockerhub-namespace>/apython_lb:latest

curl -sk https://localhost:8443/health
```

Then register a backend through the management API:

```bash
curl -sk -X POST https://localhost:8443/v1/manage/configs \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-backend",
    "proto": "https",
    "host": "example.com",
    "ips": "1.2.3.4,5.6.7.8",
    "hc_path": "/healthz"
  }'
```

Route a request to it by adding `@apython_lb(backend_name="my-backend", ...)`
to a Quart route — see the source repo for route decorator examples
(rate limiting, sticky sessions, URL rewriting).

## Ports & volumes

| | |
|---|---|
| Port | `443` (TLS; there is no plain-HTTP listener) |
| `/app/cert.pem`, `/app/key.pem` | TLS certificate and key — required, container will not start without them |
| `/app/data` | SQLite database (`apython_lb.db`) holding backend configs; mount a volume here to persist configs across restarts |

## Configuration

Set these as container environment variables. Defaults shown are the ones
baked into the image.

| Variable | Default | Description |
|---|---|---|
| `MODSECURITY_ENABLED` | `true` | WAF on by default; set `false` to disable |
| `APP_SECRET_KEY` | *(auto-generated)* | Sticky-session signing key — **set explicitly in production**, or sessions won't survive restarts/replicas |
| `MANAGE_BASIC_AUTH` | *(unset)* | `user:password` for HTTP Basic auth on `/v1/manage`, `/ws/configs`, `/metrics` — on top of the built-in internal-IP restriction |
| `LOG_LEVEL` | `INFO` | `INFO` = access logs only; `DEBUG` = all app logs |
| `RATE_LIMIT_REQUESTS` / `RATE_LIMIT_WINDOW_SECONDS` | `100` / `60` | App-wide rate limit |
| `HEALTH_CHECK_INTERVAL` | `60` | Seconds between backend health checks |
| `DNS_NAMESERVERS` | `1.1.1.1` | Comma-separated resolvers for backend hostname lookups |
| `LB_MAX_ATTEMPTS` | `3` | Retries per request before giving up |

The full variable reference (20+ options covering IP tracking, sticky
pinning, TLS verification, and WAF tuning) is in the project README linked
above.

## Security notes

- `/v1/manage`, `/ws/configs`, and `/metrics` are restricted to internal
  (RFC1918/loopback) source IPs by default. If you front this container with
  another proxy, every client collapses into that proxy's IP — set
  `MANAGE_BASIC_AUTH` in that case.
- Client identity is the TCP peer address; `X-Forwarded-For` is deliberately
  not trusted. Run this container as the first hop from clients.
- The image runs `pytest` as part of its own build (a dedicated Docker
  `test` stage in the source repo) and only publishes if that passes.

## Image contents

Base image: `python:3.14.2-slim`, plus `libmodsecurity-dev` for the WAF
engine (loaded via `ctypes`, no compiler needed at runtime). The `test`
build stage and its dependencies are stripped from the published image —
this is a lean production image.

## License & issues

See the [source repository](https://github.com/rackitio/apython_lb) for
license terms and to file issues or contribute.
