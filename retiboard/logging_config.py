"""
Unified process logging for RetiBoard.

Spec references:
  §2.2  — User nodes are local sovereign processes.
  §2.4  — Relay mode is the same node without local UI.
  §15   — Local HTTP listener on 127.0.0.1.
  §21   — Operational logging must not leak sensitive local access tokens.
"""

from __future__ import annotations

import logging
import logging.config
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from copy import copy
from typing import Any

from retiboard.config import LOG_BACKUP_COUNT, LOG_MAX_BYTES, LOG_PATH


VERBOSE_LOG_LEVEL = 15
DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_LOG_FORMAT = "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
RNS_LOG_PATTERN = re.compile(
    r"^\[(?P<timestamp>[^\]]+)\]\s+(?P<label>\[[^\]]+\])\s+(?P<message>.*)$"
)
RNS_LEVEL_MAP = {
    "[Critical]": logging.CRITICAL,
    "[Error]": logging.ERROR,
    "[Warning]": logging.WARNING,
    "[Notice]": logging.INFO,
    "[Info]": logging.INFO,
    "[Verbose]": VERBOSE_LOG_LEVEL,
    "[Debug]": logging.DEBUG,
    "[Extra]": logging.DEBUG,
}


logging.addLevelName(VERBOSE_LOG_LEVEL, "VERBOSE")


@dataclass(frozen=True)
class LoggingRuntime:
    """Resolved logging settings for the current process."""

    log_file: Path
    log_level: int
    log_to_console: bool
    uvicorn_log_level: str
    uvicorn_log_config: dict[str, Any]


class UnifiedFormatter(logging.Formatter):
    """Formatter that normalizes regular and Uvicorn access log records."""

    def format(self, record: logging.LogRecord) -> str:
        recordcopy = copy(record)
        if (
            recordcopy.name == "uvicorn.access"
            and isinstance(recordcopy.args, tuple)
            and len(recordcopy.args) == 5
        ):
            client_addr, method, full_path, http_version, status_code = (
                recordcopy.args
            )
            recordcopy.msg = '%s - "%s %s HTTP/%s" %s'
            recordcopy.args = (
                client_addr,
                method,
                full_path,
                http_version,
                status_code,
            )
        return super().format(recordcopy)


def resolve_log_file(log_file: str | None) -> Path:
    """Resolve the runtime log path."""
    if not log_file:
        return LOG_PATH
    return Path(log_file).expanduser()


def python_log_level(verbosity: int) -> int:
    """Map CLI verbosity to Python logging levels."""
    if verbosity >= 2:
        return logging.DEBUG
    if verbosity >= 1:
        return VERBOSE_LOG_LEVEL
    return logging.INFO


def uvicorn_log_level(verbosity: int) -> str:
    """Map CLI verbosity to Uvicorn's supported log levels."""
    if verbosity >= 2:
        return "debug"
    return "info"


def build_logging_config(
    log_file: Path,
    log_to_console: bool,
    verbosity: int,
) -> dict[str, Any]:
    """
    Build the unified logging configuration.

    The process writes all runtime logs to a rotating file by default.
    Console mirroring is optional so the local token banner can remain
    visible without persisting it to disk.
    """
    log_level = python_log_level(verbosity)
    handlers: dict[str, dict[str, Any]] = {
        "file_default": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(log_file),
            "formatter": "default",
            "encoding": "utf-8",
            "maxBytes": LOG_MAX_BYTES,
            "backupCount": LOG_BACKUP_COUNT,
            "delay": True,
        },
        "log_buffer": {
            "()": "retiboard.logging_buffer.get_log_buffer",
            "formatter": "default",
        },
    }
    root_handlers = ["file_default", "log_buffer"]

    if log_to_console:
        handlers["console_default"] = {
            "class": "logging.StreamHandler",
            "formatter": "default",
            "stream": "ext://sys.stderr",
        }
        root_handlers.append("console_default")

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "()": "retiboard.logging_config.UnifiedFormatter",
                "format": DEFAULT_LOG_FORMAT,
                "datefmt": DEFAULT_DATE_FORMAT,
            },
        },
        "handlers": handlers,
        "root": {
            "level": log_level,
            "handlers": root_handlers,
        },
        "loggers": {
            "uvicorn": {
                "level": log_level,
                "handlers": [],
                "propagate": True,
            },
            "uvicorn.error": {
                "level": log_level,
                "handlers": [],
                "propagate": True,
            },
            "uvicorn.access": {
                "level": logging.INFO,
                "handlers": [],
                "propagate": True,
            },
            "fastapi": {
                "level": log_level,
                "handlers": [],
                "propagate": True,
            },
            "retiboard.rns": {
                "level": log_level,
                "handlers": [],
                "propagate": True,
            },
        },
    }


def configure_logging(
    log_file: Path,
    log_to_console: bool,
    verbosity: int,
) -> LoggingRuntime:
    """Apply the unified process logging configuration."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    config = build_logging_config(
        log_file=log_file,
        log_to_console=log_to_console,
        verbosity=verbosity,
    )
    logging.config.dictConfig(config)
    logging.captureWarnings(True)
    return LoggingRuntime(
        log_file=log_file,
        log_level=python_log_level(verbosity),
        log_to_console=log_to_console,
        uvicorn_log_level=uvicorn_log_level(verbosity),
        uvicorn_log_config=config,
    )


def bridge_rns_log(log_line: str) -> None:
    """
    Forward a formatted Reticulum log line into Python logging.

    Reticulum's callback hook emits a preformatted line, so we strip its
    internal timestamp/level prefix and re-log the structural message via
    the shared handler tree.
    """
    message = str(log_line)
    level = logging.INFO
    match = RNS_LOG_PATTERN.match(message)
    if match:
        level = RNS_LEVEL_MAP.get(match.group("label").strip(), logging.INFO)
        message = match.group("message")

    logging.getLogger("retiboard.rns").log(level, message)


def render_access_banner(
    host: str,
    port: int,
    token: str,
    log_file: Path,
    log_to_console: bool,
    use_color: bool | None = None,
) -> str:
    if use_color is None:
        use_color = sys.stdout.isatty()

    # --- ANSI Palette ---
    CLR_BRDR  = "\033[90m" if use_color else ""   # Dim Grey Border
    CLR_LOGO  = "\033[1;32m" if use_color else "" # Bold Green Logo
    CLR_TITLE = "\033[1;37m" if use_color else "" # Bold White Title
    CLR_URL   = "\033[34m" if use_color else ""   # Blue URL
    CLR_RST   = "\033[0m" if use_color else ""

    CLR_BRDR  = "\033[90m"
    CLR_LOGO  = "\033[1;32m"
    CLR_TITLE = "\033[37m"
    CLR_URL   = "\033[33m"

    width = 80
    inner_width = width - 2 
    url = f"http://{host}:{port}?token={token}"

    # Multi-line Logo
    logo_lines = [
        r" ____      _   _ ____                      _ ",
        r"|  _ \ ___| |_(_) __ )  ___   __ _ _ __ __| |",
        r"| |_) / _ \ __| |  _ \ / _ \ / _` | '__/ _` |",
        r"|  _ <  __/ |_| | |_) | (_) | (_| | | | (_| |",
        r"|_| \_\___|\__|_|____/ \___/ \__,_|_|  \__,_|"
    ]

    def make_line(text: str, text_color: str, center: bool = False, raw_len: int = None) -> str:
        """raw_len allows us to pass strings that already have color or alignment."""
        content_len = raw_len if raw_len is not None else len(text)
        if center:
            padding = (inner_width - content_len) // 2
            left_pad = " " * padding
            right_pad = " " * (inner_width - content_len - padding)
            return f"{CLR_BRDR}│{CLR_RST}{left_pad}{text_color}{text}{CLR_RST}{right_pad}{CLR_BRDR}│{CLR_RST}"
        else:
            right_pad = " " * (inner_width - content_len - 2)
            return f"{CLR_BRDR}│{CLR_RST}  {text_color}{text}{CLR_RST}{right_pad}{CLR_BRDR}│{CLR_RST}"

    # Build the pieces
    top    = f"{CLR_BRDR}┌{'─' * inner_width}┐{CLR_RST}"
    empty  = f"{CLR_BRDR}│{' ' * inner_width}│{CLR_RST}"
    bottom = f"{CLR_BRDR}└{'─' * inner_width}┘{CLR_RST}"

    body = [top, empty]
    
    # Add Logo Lines
    for line in logo_lines:
        body.append(make_line(line, CLR_LOGO, center=True))
    
    body.append(make_line("v3.6.3 - Sovereign Imageboard", CLR_BRDR, center=True))
    body.append(empty)
    body.append(make_line("ACCESS URL:", CLR_TITLE, center=True))
    body.append(make_line(url, CLR_URL, center=True))
    body.append(empty)
    body.append(bottom)

    return "\n".join(body)
