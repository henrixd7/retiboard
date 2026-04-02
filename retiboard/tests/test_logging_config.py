from __future__ import annotations

import logging
from pathlib import Path

from retiboard.logging_config import (
    DEFAULT_DATE_FORMAT,
    DEFAULT_LOG_FORMAT,
    VERBOSE_LOG_LEVEL,
    UnifiedFormatter,
    build_logging_config,
    bridge_rns_log,
    render_access_banner,
)


class _CaptureHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_build_logging_config_defaults_to_file_only(tmp_path: Path) -> None:
    log_file = tmp_path / "retiboard.log"

    config = build_logging_config(
        log_file=log_file,
        log_to_console=False,
        verbosity=0,
    )

    assert "console_default" not in config["handlers"]
    assert config["root"]["handlers"] == ["file_default", "log_buffer"]
    assert config["loggers"]["uvicorn.access"]["handlers"] == []
    assert config["loggers"]["uvicorn.access"]["propagate"] is True
    assert config["handlers"]["file_default"]["filename"] == str(log_file)


def test_build_logging_config_adds_console_handlers_when_enabled(
    tmp_path: Path,
) -> None:
    config = build_logging_config(
        log_file=tmp_path / "retiboard.log",
        log_to_console=True,
        verbosity=1,
    )

    assert "console_default" in config["handlers"]
    assert config["root"]["handlers"] == ["file_default", "log_buffer", "console_default"]
    assert config["loggers"]["uvicorn.access"]["handlers"] == []
    assert config["loggers"]["uvicorn.access"]["propagate"] is True
    assert config["root"]["level"] == VERBOSE_LOG_LEVEL


def test_bridge_rns_log_strips_prefix_and_maps_level() -> None:
    logger = logging.getLogger("retiboard.rns")
    handler = _CaptureHandler()
    old_handlers = list(logger.handlers)
    old_level = logger.level
    old_propagate = logger.propagate

    logger.handlers = [handler]
    logger.setLevel(VERBOSE_LOG_LEVEL)
    logger.propagate = False

    try:
        bridge_rns_log("[2026-03-31 12:00:00] [Warning] Prune cycle failed")
    finally:
        logger.handlers = old_handlers
        logger.setLevel(old_level)
        logger.propagate = old_propagate

    assert len(handler.records) == 1
    assert handler.records[0].levelno == logging.WARNING
    assert handler.records[0].getMessage() == "Prune cycle failed"


def test_unified_formatter_normalizes_uvicorn_access_records() -> None:
    formatter = UnifiedFormatter(
        fmt=DEFAULT_LOG_FORMAT,
        datefmt=DEFAULT_DATE_FORMAT,
    )
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:12345", "GET", "/api/health", "1.1", 200),
        exc_info=None,
    )

    rendered = formatter.format(record)

    assert '127.0.0.1:12345 - "GET /api/health HTTP/1.1" 200' in rendered


def test_render_access_banner_includes_log_path_and_console_hint(
    tmp_path: Path,
) -> None:
    log_file = tmp_path / "retiboard.log"

    banner = render_access_banner(
        host="127.0.0.1",
        port=8787,
        token="abc123",
        log_file=log_file,
        log_to_console=False,
        use_color=False,
    )

    assert "v3.6.3 - Sovereign Imageboard" in banner
    assert "http://127.0.0.1:8787?token=abc123" in banner
