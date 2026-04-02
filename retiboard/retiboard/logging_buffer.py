"""
In-memory log buffering for RetiBoard.

Allows the backend to expose the last N log lines to the local UI via API.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List

# Store the last 500 log records.
DEFAULT_BUFFER_SIZE = 500


class LogBufferHandler(logging.Handler):
    """
    Python logging handler that stores records in a thread-safe in-memory list.
    Designed for stability in Nuitka-compiled environments.
    """
    def __init__(self, capacity: int = DEFAULT_BUFFER_SIZE):
        super().__init__()
        self._capacity = capacity
        self._buffer: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._counter = 0

    def emit(self, record: logging.LogRecord):
        try:
            name = record.name.lower()
            level = record.levelno
            
            # Filtering logic for UI HUD:
            is_error = level >= logging.ERROR
            is_rns = "retiboard.rns" in name or "sync" in name
            is_frontend = "frontend" in name
            
            if not (is_error or is_rns or is_frontend):
                return

            # Format the message now to avoid issues with record state later
            message = self.format(record)

            with self._lock:
                self._counter += 1
                log_entry = {
                    "id": f"{record.created}-{self._counter}", # Unique ID for Nuitka stability
                    "timestamp": record.created,
                    "level": record.levelname,
                    "name": record.name,
                    "message": message,
                }
                
                self._buffer.append(log_entry)
                if len(self._buffer) > self._capacity:
                    self._buffer = self._buffer[-self._capacity:]
        except Exception:
            self.handleError(record)

    def get_logs(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._buffer)


# Global singleton instance of the handler.
_buffer_handler = LogBufferHandler()


def get_log_buffer() -> LogBufferHandler:
    """Accessor for the log buffer singleton."""
    return _buffer_handler
