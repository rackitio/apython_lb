import logging
import logging.config
import os

VALID_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}


def resolve_log_level(level: str = None) -> str:
    """Resolve the log level from the argument or LOG_LEVEL env var."""
    level = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    if level not in VALID_LEVELS:
        logging.getLogger(__name__).debug(f"Invalid LOG_LEVEL {level!r}, falling back to INFO")
        return "INFO"
    return level


def build_logging_config(level: str = None) -> dict:
    level = resolve_log_level(level)
    # httpx emits one useful "HTTP Request: ..." line per request at INFO. Hide
    # that behind DEBUG (health checks/proxied requests would flood the access
    # log), matching the app-log rule.
    third_party_level = "DEBUG" if level == "DEBUG" else "WARNING"
    # These libraries emit high-volume protocol/query internals at DEBUG that
    # bury the app's own logs. Pin them to ERROR at all levels so only genuine
    # failures surface — even when LOG_LEVEL=DEBUG.
    noisy_error_only = ("httpcore", "hpack", "aiosqlite")
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {
                "format": "%(asctime)s [%(process)d] [%(levelname)s] %(name)s:%(lineno)d - %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "standard",
                "level": "DEBUG",
                "stream": "ext://sys.stdout",
            },
        },
        "loggers": {
            "quart.app": {
                "handlers": ["console"],
                "level": level,
                "propagate": False,
            },
            "quart.serving": {
                "handlers": ["console"],
                "level": level,
                "propagate": False,
            },
            # Hypercorn's own loggers stay at INFO regardless of LOG_LEVEL so
            # access logs and startup messages are always emitted exactly once.
            "hypercorn.error": {
                "handlers": ["console"],
                "level": "INFO",
                "propagate": False,
            },
            "hypercorn.access": {
                "handlers": ["console"],
                "level": "INFO",
                "propagate": False,
            },
            # httpx logs one request line at INFO; only surface it at DEBUG.
            "httpx": {
                "handlers": ["console"],
                "level": third_party_level,
                "propagate": False,
            },
            # httpcore/hpack/aiosqlite spew protocol and query internals at DEBUG;
            # pinned to ERROR at all levels (see noisy_error_only above).
            **{
                name: {
                    "handlers": ["console"],
                    "level": "ERROR",
                    "propagate": False,
                }
                for name in noisy_error_only
            },
        },
        # All app loggers (classes.*, decorators.*, routes.*, main) inherit
        # this level via propagation — they need no explicit entries.
        "root": {
            "handlers": ["console"],
            "level": level,
        },
    }


def configure_logging(level: str = None):
    """
    Apply the logging config. Must be called at startup (main.py) — defining
    the dict alone does nothing. Hypercorn applies the same dict again later
    via hypercorn_config.py (logconfig_dict), so its lazy Logger creation
    can't clobber this config.
    """
    logging.config.dictConfig(build_logging_config(level))


# Kept for backwards compatibility with code that imports the dict directly.
LOGGING_CONFIG = build_logging_config()
