FROM debian:trixie-slim AS base

ENV PATH=/usr/local/bin:$PATH

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

# Runtime deps for the Python build below — kept manually-marked so the
# later apt-mark/purge dance (which drops build-only packages) doesn't
# sweep these up.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    netbase \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# No official Python Docker image ships a free-threaded ("t") build for any
# version (checked docker-library/python's versions.json — not for 3.13,
# 3.14, or the 3.15 release candidate), and 3.15 itself isn't GA yet
# (3.15.0rc2, due 2026-10-01). So we compile free-threaded CPython 3.14.7
# (latest stable release) from source instead, following the official
# docker-library/python recipe with --disable-gil added.
ENV PYTHON_VERSION=3.14.7
ENV PYTHON_SHA256=3b48dac8fb59f62eaa67ac83c1eb12bda1b7a08406dd286e252c11a66be27f81

RUN set -eux; \
    savedAptMark="$(apt-mark showmanual)"; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        dpkg-dev \
        g++ \
        gcc \
        gnupg \
        libbluetooth-dev \
        libbz2-dev \
        libc6-dev \
        libdb-dev \
        libffi-dev \
        libgdbm-dev \
        liblzma-dev \
        libncursesw5-dev \
        libreadline-dev \
        libsqlite3-dev \
        libssl-dev \
        libzstd-dev \
        make \
        tk-dev \
        uuid-dev \
        wget \
        xz-utils \
        zlib1g-dev \
    ; \
    \
    wget -O python.tar.xz "https://www.python.org/ftp/python/${PYTHON_VERSION}/Python-$PYTHON_VERSION.tar.xz"; \
    echo "$PYTHON_SHA256 *python.tar.xz" | sha256sum -c -; \
    mkdir -p /usr/src/python; \
    tar --extract --directory /usr/src/python --strip-components=1 --file python.tar.xz; \
    rm python.tar.xz; \
    \
    cd /usr/src/python; \
    gnuArch="$(dpkg-architecture --query DEB_BUILD_GNU_TYPE)"; \
    ./configure \
        --build="$gnuArch" \
        --disable-gil \
        --enable-loadable-sqlite-extensions \
        --enable-optimizations \
        --enable-option-checking=fatal \
        --enable-shared \
        $(test "${gnuArch%%-*}" != 'riscv64' && echo '--with-lto') \
        --with-ensurepip \
    ; \
    nproc="$(nproc)"; \
    EXTRA_CFLAGS="$(dpkg-buildflags --get CFLAGS)"; \
    LDFLAGS="$(dpkg-buildflags --get LDFLAGS)"; \
    LDFLAGS="${LDFLAGS:-} -Wl,--strip-all"; \
    make -j "$nproc" \
        "EXTRA_CFLAGS=${EXTRA_CFLAGS:-}" \
        "LDFLAGS=${LDFLAGS:-}" \
    ; \
    rm python; \
    make -j "$nproc" \
        "EXTRA_CFLAGS=${EXTRA_CFLAGS:-}" \
        "LDFLAGS=${LDFLAGS:-} -Wl,-rpath='\$\$ORIGIN/../lib'" \
        python \
    ; \
    make install; \
    \
    cd /; \
    rm -rf /usr/src/python; \
    \
    find /usr/local -depth \
        \( \
            \( -type d -a \( -name test -o -name tests -o -name idle_test \) \) \
            -o \( -type f -a \( -name '*.pyc' -o -name '*.pyo' -o -name 'libpython*.a' \) \) \
        \) -exec rm -rf '{}' + \
    ; \
    \
    ldconfig; \
    \
    apt-mark auto '.*' > /dev/null; \
    apt-mark manual $savedAptMark; \
    find /usr/local -type f -executable -not \( -name '*tkinter*' \) -exec ldd '{}' ';' \
        | awk '/=>/ { so = $(NF-1); if (index(so, "/usr/local/") == 1) { next }; gsub("^/(usr/)?", "", so); printf "*%s\n", so }' \
        | sort -u \
        | xargs -rt dpkg-query --search \
        | awk 'sub(":$", "", $1) { print $1 }' \
        | sort -u \
        | xargs -r apt-mark manual \
    ; \
    apt-get purge -y --auto-remove -o APT::AutoRemove::RecommendsImportant=false; \
    rm -rf /var/lib/apt/lists/*; \
    \
    python3 --version; \
    pip3 --version; \
    python3 -c "import sys; assert not sys._is_gil_enabled(), 'free-threaded build check failed: GIL is enabled'"

RUN set -eux; \
    for src in idle3 pip3 pydoc3 python3 python3-config; do \
        dst="$(echo "$src" | tr -d 3)"; \
        [ -s "/usr/local/bin/$src" ]; \
        [ ! -e "/usr/local/bin/$dst" ]; \
        ln -svT "$src" "/usr/local/bin/$dst"; \
    done

# libmodsecurity3 runtime — loaded at startup via ctypes when MODSECURITY_ENABLED=true.
# No compiler or dev headers needed; ctypes calls the C API in the shared library directly.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libmodsecurity-dev \
    && rm -rf /var/lib/apt/lists/*

ENV MODSECURITY_ENABLED=true
ENV MODSECURITY_RULES_FILE=/app/modsecurity.conf

# Read by hypercorn_config.py -- kept below the CPython compile step so
# changing these doesn't bust that (expensive) build cache layer.
ENV HYPERCORN_WORKERS=1
ENV HYPERCORN_WORKER_CLASS=asyncio

COPY requirements.txt requirements-dev.txt .

# aioquic and pylsqpack (hypercorn's HTTP/3 stack) both hard-code Python's
# Limited API in their setup.py, which free-threaded CPython doesn't support
# at all (Python.h itself errors: "The limited API is not currently
# supported in the free-threaded build") — so neither their upstream wheels
# nor a stock source build work here. Fetch both and rebuild against the
# full API instead, same as every other native dependency in this image
# (cryptography, pycares, cffi all already ship real cp314t wheels). Revisit
# once upstream does too:
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
# this step alongside a compiler.
RUN set -eux; \
    savedAptMark="$(apt-mark showmanual)"; \
    apt-get update; \
    apt-get install -y --no-install-recommends build-essential libssl-dev; \
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
#     cp314t wheels and doesn't trigger CPython's GIL-reenable at all
#     (confirmed empirically -- pip install uvloop; python3 -c "import
#     uvloop" leaves sys._is_gil_enabled() False).
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
