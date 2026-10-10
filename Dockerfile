# Free-threaded CPython 3.15.0, built by github.com/rackitio/pythont (see
# that repo for why this isn't just FROM python:3.15-slim -- no official
# Python image ships a free-threaded build -- and for how to bump the
# version here once pythont publishes a new one). Pinned to an exact
# X.Y.Z tag rather than pythont's stable/latest/X.Y channels, which move
# and can jump a minor version out from under this image without warning
# -- see pythont's README for the tag policy.
FROM rackitio/pythont:3.15.0 AS base

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
ENV HYPERCORN_WORKERS=1
ENV HYPERCORN_WORKER_CLASS=asyncio

WORKDIR /app

# libmodsecurity3 runtime — loaded at startup via ctypes when MODSECURITY_ENABLED=true.
# No compiler or dev headers needed; ctypes calls the C API in the shared library directly.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libmodsecurity-dev \
    && rm -rf /var/lib/apt/lists/*

ENV MODSECURITY_ENABLED=true
ENV MODSECURITY_RULES_FILE=/app/modsecurity.conf

COPY requirements.txt requirements-dev.txt .

# aioquic and pylsqpack (hypercorn's HTTP/3 stack) both hard-code Python's
# Limited API in their setup.py, which free-threaded CPython doesn't support
# at all (Python.h itself errors: "The limited API is not currently
# supported in the free-threaded build") — so neither their upstream wheels
# nor a stock source build work here. Fetch both and rebuild against the
# full API instead, same as every other native dependency in this image
# (cryptography and cffi already ship real cp315t wheels; pycares doesn't
# yet, hence cmake below -- that's just a missing wheel, unrelated to this
# Limited API problem). Revisit once upstream does too:
# https://github.com/aiortc/pylsqpack https://github.com/aiortc/aioquic
#
# Rebuilding fixes the compile, but not full free-threading: aioquic._buffer
# doesn't declare itself GIL-safe, so importing it (unconditional here, since
# QUIC is always bound) makes CPython silently re-enable the GIL for the
# whole process at startup. Free-threading still buys nothing over regular
# Python while HTTP/3 is on. Since hypercorn's own request handling is
# asyncio-based (single-threaded per worker) rather than OS-thread-parallel,
# this mainly affects true CPU-bound multi-threaded code elsewhere in the
# app, if any is ever added.
RUN set -eux; \
    pip download --no-binary=:all: --no-deps --no-cache-dir -d /tmp/pylsqpack-src 'pylsqpack==0.3.24'; \
    mkdir -p /tmp/pylsqpack-build; \
    tar --extract --directory /tmp/pylsqpack-build --strip-components=1 --file /tmp/pylsqpack-src/pylsqpack-0.3.24.tar.gz; \
    rm -rf /tmp/pylsqpack-src; \
    pip download --no-binary=:all: --no-deps --no-cache-dir -d /tmp/aioquic-src 'aioquic==1.3.0'; \
    mkdir -p /tmp/aioquic-build; \
    tar --extract --directory /tmp/aioquic-build --strip-components=1 --file /tmp/aioquic-src/aioquic-1.3.0.tar.gz; \
    rm -rf /tmp/aioquic-src

COPY <<'PYSETUP' /tmp/pylsqpack-build/setup.py
import os.path

import setuptools

include_dirs = [
    os.path.join("vendor", "ls-qpack"),
    os.path.join("vendor", "ls-qpack", "deps", "xxhash"),
]

setuptools.setup(
    ext_modules=[
        setuptools.Extension(
            "pylsqpack._binding",
            extra_compile_args=["-std=c99"],
            include_dirs=include_dirs,
            sources=[
                "src/pylsqpack/binding.c",
                "vendor/ls-qpack/lsqpack.c",
                "vendor/ls-qpack/deps/xxhash/xxhash.c",
            ],
        ),
    ],
)
PYSETUP

COPY <<'PYSETUP' /tmp/aioquic-build/setup.py
import setuptools

setuptools.setup(
    ext_modules=[
        setuptools.Extension(
            "aioquic._buffer",
            extra_compile_args=["-std=c99"],
            sources=["src/aioquic/_buffer.c"],
        ),
        setuptools.Extension(
            "aioquic._crypto",
            extra_compile_args=["-std=c99"],
            libraries=["crypto"],
            sources=["src/aioquic/_crypto.c"],
        ),
    ],
)
PYSETUP

# aioquic's _crypto extension links against libcrypto (-lcrypto), so
# libssl-dev (purged after the Python build above) needs to come back for
# this step alongside a compiler. cmake is for pycares (aiodns's C
# extension): no cp315t wheel exists yet, so pip builds it from source,
# and pycares' own build now vendors c-ares via CMake.
RUN set -eux; \
    savedAptMark="$(apt-mark showmanual)"; \
    apt-get update; \
    apt-get install -y --no-install-recommends build-essential cmake libssl-dev; \
    pip install --no-cache-dir --upgrade pip; \
    pip install --no-cache-dir /tmp/pylsqpack-build /tmp/aioquic-build; \
    rm -rf /tmp/pylsqpack-build /tmp/aioquic-build; \
    pip install --no-cache-dir -r requirements.txt; \
    apt-mark auto '.*' > /dev/null; \
    apt-mark manual $savedAptMark; \
    apt-get purge -y --auto-remove -o APT::AutoRemove::RecommendsImportant=false; \
    rm -rf /var/lib/apt/lists/*

COPY . .
# data/ is dockerignored (runtime state, mounted as a volume in production);
# the directory must still exist for the default SQLITE_DB path.
RUN mkdir -p /app/data

FROM base AS test
RUN pip install --no-cache-dir -r requirements-dev.txt
RUN pytest tests

FROM base AS final
RUN rm -rf /app/tests /app/requirements-dev.txt

# Without this, CPython auto-reenables the GIL process-wide the moment
# aioquic._buffer/aioquic._crypto load (see the pip install step above) —
# silently giving up free-threading as soon as HTTP/3 starts, since
# --quic-bind is unconditional below. Forcing it off is safe for how this
# image actually runs QUIC: neither aioquic extension locks its internal
# per-instance C state (Buffer.pos, AEAD's OpenSSL contexts), so it's only
# unsafe if the same instance is touched by two OS threads at once. Verified
# this holds for both worker_class options HYPERCORN_WORKER_CLASS can select
# (hypercorn_config.py; trio is deliberately not offered — see the comment
# there — so it isn't considered here):
#   - asyncio: UDPServer/QuicProtocol (hypercorn/asyncio/udp_server.py) feed
#     incoming datagrams through one asyncio.Queue, drained by a single
#     coroutine on one event-loop thread -- no ThreadPoolExecutor or
#     run_in_executor anywhere in that path.
#   - uvloop: hypercorn's uvloop_worker reuses that exact same asyncio code
#     path, just with a different (still single-threaded) loop underneath.
#     uvloop itself also isn't a concern here regardless: it ships real
#     cp314t/cp315t wheels and doesn't trigger CPython's GIL-reenable at
#     all (confirmed empirically on both -- python3 -c "import uvloop"
#     leaves sys._is_gil_enabled() False).
# --workers scales by spawning separate OS processes (multiprocessing,
# "spawn" start method), not threads, so instances are never shared across
# workers, and each process starts fresh with this same PYTHON_GIL=0.
# Confirmed via /proc/<worker-pid>/environ, not just the CLI supervisor
# process. This holds only as long as that stays true -- a future Hypercorn
# or aioquic version, or a switch to a threaded deployment model, could
# invalidate it silently. Neither aioquic nor uvloop has audited or
# declared itself free-threading-safe via CPython's own mechanism for it,
# so this is us reading their source (and, for uvloop, testing directly),
# not an upstream guarantee.
ENV PYTHON_GIL=0

EXPOSE 443/tcp
EXPOSE 443/udp
CMD ["hypercorn", "main:app", "--bind", "0.0.0.0:443", \
     "--quic-bind", "0.0.0.0:443", \
     "--keyfile", "/app/key.pem", \
     "--certfile", "/app/cert.pem", \
     "--config", "file:/app/hypercorn_config.py", \
     "--access-logfile", "-"]
