"""Chunk pre-validation.

A chunk must not be admitted into the reassembly buffer until the structural
manifest says it belongs there and the encrypted bytes hash correctly.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Mapping

from .models import ChunkManifest, ChunkManifestEntry


class ChunkValidationError(ValueError):
    """Raised when a received encrypted chunk fails structural validation."""


@dataclass
class ChunkValidator:
    """Stateless validator for received encrypted chunk bytes."""

    def prevalidate(
        self,
        *,
        manifest: ChunkManifest,
        entries_by_index: Mapping[int, ChunkManifestEntry],
        chunk_index: int,
        peer_lxmf_hash: str,
        assigned_peer_lxmf_hash: str,
        data: bytes,
    ) -> ChunkManifestEntry:
        if chunk_index < 0 or chunk_index >= manifest.chunk_count:
            raise ChunkValidationError(
                f"Invalid chunk_index {chunk_index}; expected 0..{manifest.chunk_count - 1}"
            )

        if assigned_peer_lxmf_hash and peer_lxmf_hash != assigned_peer_lxmf_hash:
            raise ChunkValidationError(
                f"Chunk {chunk_index} came from unexpected peer {peer_lxmf_hash[:16]}"
            )

        entry = entries_by_index.get(chunk_index)
        if entry is None:
            raise ChunkValidationError(
                f"Manifest missing entry for chunk {chunk_index}"
            )

        if len(data) != entry.size:
            raise ChunkValidationError(
                f"Chunk {chunk_index} size mismatch: expected {entry.size}, got {len(data)}"
            )

        computed = hashlib.sha256(data).hexdigest()
        if computed != entry.chunk_hash:
            raise ChunkValidationError(
                f"Chunk {chunk_index} hash mismatch: expected {entry.chunk_hash}, got {computed}"
            )

        expected_end = entry.offset + entry.size
        if expected_end > manifest.blob_size:
            raise ChunkValidationError(
                f"Chunk {chunk_index} overflows blob_size={manifest.blob_size}"
            )

        return entry
