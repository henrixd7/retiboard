"""
Opaque payload storage for RetiBoard.

Spec references:
    §3.2 — Encrypted payload: AES-GCM encrypted envelope stored as
           opaque <content_hash>.bin. "Stored as opaque .bin (no decryption in backend)"
    §4   — Disk layout: /boards/<board_id>/payloads/<content_hash>.bin

Design invariants:
    - This module is a DUMB BLOB STORE. It writes bytes, reads bytes,
      deletes files. That's it.
    - It NEVER opens, parses, decrypts, inspects, or validates the
      contents of .bin files.
    - It NEVER infers MIME types, generates thumbnails, or reads headers.
    - The only validation is content_hash verification: the caller
      provides the expected hash and the blob; we verify the hash
      matches the blob to prevent corruption/tampering (§6.2).
    - Content-addressed: filename = <content_hash>.bin

    If you're tempted to add any content-aware logic here — STOP.
    That violates §3.2 and the opacity invariant.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

from retiboard.db.database import board_payloads_dir, board_chunk_cache_dir


def payload_path(board_id: str, content_hash: str) -> Path:
    """
    Return the filesystem path for a payload blob.

    Layout: ~/.retiboard/boards/<board_id>/payloads/<content_hash>.bin
    """
    return board_payloads_dir(board_id) / f"{content_hash}.bin"


def write_payload(
    board_id: str,
    content_hash: str,
    data: bytes,
    verify_hash: bool = True,
) -> Path:
    """
    Store an opaque encrypted payload blob.

    Args:
        board_id: The board this payload belongs to.
        content_hash: Expected SHA-256 hex digest of the data.
        data: Raw bytes (nonce + ciphertext + tag). We don't care what's inside.
        verify_hash: If True (default), verify data matches content_hash.
                     Per §6.2: "Verify content_hash matches".

    Returns:
        Path to the written file.

    Raises:
        ValueError: If verify_hash=True and the hash doesn't match.
        OSError: If the write fails (disk full, permissions, etc.).
    """
    if verify_hash:
        computed = hashlib.sha256(data).hexdigest()
        if computed != content_hash:
            raise ValueError(
                f"Payload hash mismatch: expected {content_hash}, "
                f"got {computed}. Rejecting corrupted/tampered payload."
            )

    target = payload_path(board_id, content_hash)

    # Ensure the payloads directory exists.
    target.parent.mkdir(parents=True, exist_ok=True)

    # Atomic-ish write: write to temp, then rename.
    # This prevents partial files on crash.
    tmp = target.with_suffix(".tmp")
    try:
        tmp.write_bytes(data)
        tmp.rename(target)
    except Exception:
        # Clean up temp file on failure.
        tmp.unlink(missing_ok=True)
        raise

    return target


def read_payload(board_id: str, content_hash: str) -> Optional[bytes]:
    """
    Read an opaque payload blob.

    Returns the raw bytes or None if the file doesn't exist.
    We return the bytes as-is — no parsing, no decryption.
    The frontend (and ONLY the frontend) decrypts these.
    """
    target = payload_path(board_id, content_hash)
    if not target.exists():
        return None
    return target.read_bytes()


def delete_payload(board_id: str, content_hash: str) -> bool:
    """
    Delete a payload blob.

    Returns True if the file existed and was deleted, False if it
    was already gone (idempotent — safe for pruning).
    """
    target = payload_path(board_id, content_hash)
    if target.exists():
        target.unlink()
        return True
    return False


def delete_payloads_bulk(board_id: str, content_hashes: list[str]) -> int:
    """
    Delete multiple payload blobs at once.

    Used by the pruner when deleting abandoned threads (§4).
    Returns the count of files actually deleted.
    """
    deleted = 0
    for ch in content_hashes:
        if delete_payload(board_id, ch):
            deleted += 1
    return deleted


def payload_exists(board_id: str, content_hash: str) -> bool:
    """Check if a payload blob exists on disk."""
    return payload_path(board_id, content_hash).exists()


def get_payload_size(board_id: str, content_hash: str) -> Optional[int]:
    """
    Return the size in bytes of a payload blob, or None if missing.

    This reads file metadata only — not the file contents.
    """
    target = payload_path(board_id, content_hash)
    if not target.exists():
        return None
    return target.stat().st_size



def chunk_cache_dir(board_id: str, blob_hash: str) -> Path:
    """Return the ephemeral chunk-cache directory for one blob."""
    return board_chunk_cache_dir(board_id) / blob_hash



def chunk_part_path(board_id: str, blob_hash: str, chunk_index: int) -> Path:
    """Return the path for one staged verified chunk file."""
    return chunk_cache_dir(board_id, blob_hash) / f"{chunk_index}.part"



def chunk_assembly_path(board_id: str, blob_hash: str) -> Path:
    """Return the path for the random-access assembly temp file."""
    return chunk_cache_dir(board_id, blob_hash) / "assembly.tmp"



def delete_chunk_cache(board_id: str, blob_hash: str) -> bool:
    """Delete all ephemeral chunk-cache state for one blob."""
    cache_dir = chunk_cache_dir(board_id, blob_hash)
    if not cache_dir.exists():
        return False
    for child in sorted(cache_dir.rglob('*'), reverse=True):
        if child.is_file() or child.is_symlink():
            child.unlink(missing_ok=True)
        elif child.is_dir():
            child.rmdir()
    cache_dir.rmdir()
    return True



def delete_chunk_cache_bulk(board_id: str, blob_hashes: list[str]) -> int:
    """Delete ephemeral chunk cache trees for multiple blobs."""
    deleted = 0
    for blob_hash in blob_hashes:
        if delete_chunk_cache(board_id, blob_hash):
            deleted += 1
    return deleted
