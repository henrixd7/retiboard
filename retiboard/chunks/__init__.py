"""Chunked transport helpers for large opaque encrypted payloads.

Phase 1 scope:
    - Deterministic post-encryption splitting
    - Structural manifest models
    - Chunk pre-validation
    - Memory-efficient sparse/random-access reassembly

Design invariant:
    Chunking is a transport optimization only. The canonical object identity
    remains SHA-256(encrypted_blob) per spec §3.2/§6.1/§6.2.
"""

from .models import (
    BlobKind,
    ChunkFetchState,
    ChunkManifest,
    ChunkManifestEntry,
    ChunkFetchSession,
)
from .chunker import build_chunk_manifest, split_encrypted_blob
from .validator import ChunkValidationError, ChunkValidator
from .reassembly import ReassemblyBuffer

__all__ = [
    "BlobKind",
    "ChunkFetchState",
    "ChunkManifest",
    "ChunkManifestEntry",
    "ChunkFetchSession",
    "build_chunk_manifest",
    "split_encrypted_blob",
    "ChunkValidationError",
    "ChunkValidator",
    "ReassemblyBuffer",
]
