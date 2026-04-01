"""
Global settings management for RetiBoard.

Spec references:
    §2.2  — User nodes are local sovereign processes.
    §4    — Local storage and pruning rules.

Design:
    Settings are persisted to a simple JSON file in RETIBOARD_HOME.
    This includes global preferences like the disk quota.
"""

import json
from typing import Any, Dict

from retiboard.config import RETIBOARD_HOME

SETTINGS_PATH = RETIBOARD_HOME / "settings.json"

DEFAULT_SETTINGS = {
    "global_storage_limit_mb": 1024,  # 1 GB
}


class GlobalSettings:
    """
    Manages global user settings persisted to disk.
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(GlobalSettings, cls).__new__(cls)
            cls._instance._load()
        return cls._instance

    def _load(self):
        self._data = DEFAULT_SETTINGS.copy()
        if SETTINGS_PATH.exists():
            try:
                with open(SETTINGS_PATH, "r") as f:
                    disk_data = json.load(f)
                    self._data.update(disk_data)
            except Exception:
                # Fallback to defaults if corrupted
                pass

    def _save(self):
        try:
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(SETTINGS_PATH, "w") as f:
                json.dump(self._data, f, indent=4)
        except Exception:
            pass

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any):
        self._data[key] = value
        self._save()

    def update(self, delta: Dict[str, Any]):
        self._data.update(delta)
        self._save()

    def to_dict(self) -> Dict[str, Any]:
        return self._data.copy()


def get_settings() -> GlobalSettings:
    """Accessor for the global settings singleton."""
    return GlobalSettings()
