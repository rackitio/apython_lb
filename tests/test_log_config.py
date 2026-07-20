import logging

from log_config import build_logging_config, configure_logging, resolve_log_level


def test_resolve_log_level_from_env(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "debug")
    assert resolve_log_level() == "DEBUG"

    monkeypatch.delenv("LOG_LEVEL")
    assert resolve_log_level() == "INFO"

    # Explicit argument wins over env
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    assert resolve_log_level("WARNING") == "WARNING"


def test_invalid_level_falls_back_to_info(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "VERBOSE")
    assert resolve_log_level() == "INFO"


def test_build_config_sets_root_and_quart_levels():
    config = build_logging_config("DEBUG")
    assert config["root"]["level"] == "DEBUG"
    assert config["loggers"]["quart.app"]["level"] == "DEBUG"
    assert config["disable_existing_loggers"] is False


def test_httpx_noise_suppressed_unless_debug():
    # httpx logs one request line per call at INFO — it must stay quiet at INFO
    # and only surface when LOG_LEVEL is DEBUG.
    info_cfg = build_logging_config("INFO")["loggers"]["httpx"]
    assert info_cfg["level"] == "WARNING"
    assert info_cfg["propagate"] is False

    assert build_logging_config("DEBUG")["loggers"]["httpx"]["level"] == "DEBUG"
    assert build_logging_config("ERROR")["loggers"]["httpx"]["level"] == "WARNING"


def test_noisy_libs_pinned_to_error_at_every_level():
    # httpcore/hpack/aiosqlite spew protocol and query internals at DEBUG — too
    # noisy even when debugging the app, so they stay at ERROR regardless of
    # LOG_LEVEL.
    for noisy in ("httpcore", "hpack", "aiosqlite"):
        for level in ("INFO", "DEBUG", "ERROR"):
            cfg = build_logging_config(level)["loggers"][noisy]
            assert cfg["level"] == "ERROR"
            assert cfg["propagate"] is False


def test_configure_logging_controls_app_logger_records(caplog):
    # App loggers (classes.*, decorators.*) have no explicit config and must
    # inherit their effective level from root.
    configure_logging("INFO")
    app_logger = logging.getLogger("classes.some_module")
    assert not app_logger.isEnabledFor(logging.DEBUG)
    assert app_logger.isEnabledFor(logging.INFO)
    assert app_logger.isEnabledFor(logging.ERROR)

    configure_logging("DEBUG")
    assert app_logger.isEnabledFor(logging.DEBUG)

    configure_logging("ERROR")
    assert not app_logger.isEnabledFor(logging.INFO)
    assert app_logger.isEnabledFor(logging.ERROR)

    # Restore default so other tests are unaffected
    configure_logging("INFO")
