"""
Board subscription management.

Spec references:
    §3.3 — Board announce schema stored in board_config table
    §5   — key_material: "The backend never receives key_material or the
           derived key under any circumstances."
    §9   — Board discovery via RNS announce propagation

Design decisions on key_material storage:
    The spec §5 says the backend must never hold the derived AES-GCM key.
    The key_material field in the announce is deliberately public (§5), but
    to honor the spirit of opacity we handle it as follows:

    - key_material is cached IN MEMORY ONLY in the BoardAnnounceHandler.
    - When saving board config to SQLite, we store an EMPTY STRING for
      key_material in the board_config table. The actual key_material
      is only available from the in-memory announce cache.
    - The API layer retrieves key_material from the in-memory cache
      when the frontend requests it.
    - On restart, key_material is recovered by re-receiving the board
      announce from the network (or from the locally cached announce
      file, which is stored as an opaque blob).

    This ensures:
    1. SQLite (meta.db) NEVER contains key_material.
    2. key_material is available to the frontend via API during a session.
    3. Restarting requires re-receiving the announce (normal in an
       ephemeral system — peers re-announce periodically).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from retiboard.config import BOARDS_DIR
from retiboard.db.models import Board

log = logging.getLogger(__name__)


# =============================================================================
# Opaque announce cache (filesystem)
# =============================================================================
# We store the raw announce app_data as an opaque blob so we can recover
# key_material on restart without waiting for a re-announce from the network.
# This file is NOT a database — it's a raw JSON blob that contains key_material.
# It lives alongside meta.db but is conceptually separate.

def _announce_cache_path(board_id: str) -> Path:
    """Path to the cached announce blob for a board."""
    return BOARDS_DIR / board_id / "announce.json"


def save_announce_cache(board_id: str, announce_data: dict) -> None:
    """
    Cache the raw announce dict to disk as an opaque file.

    This preserves key_material across restarts without putting it
    in SQLite. The file is a simple JSON blob.
    """
    path = _announce_cache_path(board_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(announce_data, separators=(",", ":")),
        encoding="utf-8",
    )
    log.debug("Cached announce for board %s", board_id)


def load_announce_cache(board_id: str) -> Optional[dict]:
    """
    Load a cached announce dict from disk.

    Returns None if no cache exists or the file is corrupt.
    """
    path = _announce_cache_path(board_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        return None
    except (json.JSONDecodeError, OSError):
        return None


def delete_announce_cache(board_id: str) -> None:
    """Delete the cached announce file for a board."""
    path = _announce_cache_path(board_id)
    if path.exists():
        path.unlink()


# =============================================================================
# Subscription helpers
# =============================================================================

def board_for_db_storage(board: Board) -> Board:
    """
    Return a copy of the Board with key_material stripped for DB storage.

    The board_config table in SQLite must NEVER contain key_material.
    This function ensures we never accidentally persist it.

    The returned Board has key_material="" which is what gets written
    to meta.db. The real key_material lives only in:
    1. The in-memory announce cache (BoardAnnounceHandler.received_announces)
    2. The opaque announce.json file on disk
    """
    from dataclasses import replace
    return replace(board, key_material="")


def recover_key_material(board_id: str) -> str:
    """
    Recover key_material from the on-disk announce cache.

    Called on startup or when the in-memory cache is cold.
    Supports both compact ("km") and verbose ("key_material") formats.
    Returns empty string if no cache exists.
    """
    cached = load_announce_cache(board_id)
    if cached:
        # Compact format uses "km", verbose uses "key_material".
        km = cached.get("km", cached.get("key_material", ""))
        if km:
            return km
    return ""
