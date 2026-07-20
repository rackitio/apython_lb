FROM python:3.14.2-slim AS base

ENV SQLITE_DB=/app/data/apython_lb.db
ENV DNS_NAMESERVERS="1.1.1.1"
ENV IP_TRACKER_404_THRESHOLD=4
ENV IP_TRACKER_WINDOW_SECONDS=120
ENV IP_TRACKER_PENALTY_DURATION=300
ENV RATE_LIMIT_REQUESTS=100
ENV RATE_LIMIT_WINDOW_SECONDS=60
ENV LOG_LEVEL=INFO
ENV HEALTH_CHECK_INTERVAL=60
ENV CONFIG_POLL_INTERVAL=10
ENV LB_MAX_ATTEMPTS=3
ENV LB_UPSTREAM_HTTP2=true
ENV LB_UPSTREAM_VERIFY_TLS=true
ENV LB_UPSTREAM_TIMEOUT_SECONDS=30

WORKDIR /app

# libmodsecurity3 runtime — loaded at startup via ctypes when MODSECURITY_ENABLED=true.
# No compiler or dev headers needed; ctypes calls the C API in the shared library directly.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libmodsecurity-dev \
    && rm -rf /var/lib/apt/lists/*

ENV MODSECURITY_ENABLED=true
ENV MODSECURITY_RULES_FILE=/app/modsecurity.conf

COPY requirements.txt requirements-dev.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . .
# data/ is dockerignored (runtime state, mounted as a volume in production);
# the directory must still exist for the default SQLITE_DB path.
RUN mkdir -p /app/data

FROM base AS test
RUN pip install --no-cache-dir -r requirements-dev.txt
RUN pytest tests

FROM base AS final
RUN rm -rf /app/tests /app/requirements-dev.txt

EXPOSE 443
CMD ["hypercorn", "main:app", "--bind", "0.0.0.0:443", \
     "--keyfile", "/app/key.pem", \
     "--certfile", "/app/cert.pem", \
     "--config", "file:/app/hypercorn_config.py", \
     "--access-logfile", "-"]
