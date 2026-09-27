# Hypercorn config, loaded via: hypercorn --config file:hypercorn_config.py
#
# Hypercorn creates its Logger lazily, after the app module has imported and
# the lifespan startup has run. With the old `--log-config log_config.ini`
# that meant fileConfig re-applied root at INFO and clobbered whatever
# main.py had configured — debug/error levels set via LOG_LEVEL never took
# effect at request time. Feeding the same dict used by main.py through
# logconfig_dict makes hypercorn's late application a no-op.
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from log_config import build_logging_config

logconfig_dict = build_logging_config()

# CLI flags don't expand env vars in the Dockerfile's exec-form CMD, so
# these are set here instead -- Hypercorn's --config file: loader applies
# any attribute matching a Config field, same as logconfig_dict above.
# worker_class must be "asyncio" (default) or "uvloop" -- both are installed
# (see requirements.txt) and run QUIC on a single event-loop thread with no
# thread pool, which is what makes PYTHON_GIL=0 safe (see the Dockerfile
# comment above ENV PYTHON_GIL=0). --workers spawns separate OS processes,
# each starting fresh with this same environment, so that guarantee holds
# per-worker regardless of count.
# Not "trio": aiosqlite (classes/sqlite_db.py) calls asyncio.get_event_loop()
# internally, which doesn't exist under trio's runtime -- the app fails at
# startup regardless of GIL/free-threading, so it isn't offered as a choice.
workers = int(os.environ.get("HYPERCORN_WORKERS", "1"))
worker_class = os.environ.get("HYPERCORN_WORKER_CLASS", "asyncio")
