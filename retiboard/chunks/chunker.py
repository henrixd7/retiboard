"""Deterministic post-encryption chunking for canonical encrypted blobs.

Spec grounding:
    - §3.2 / §6.1 / §6.2 define the canonical payload object as the encrypted
      blob whose SHA-256 becomes content_hash / attachment_content_hash.
    - Therefore split MUST happen *after* encryption, never before.

This module does not know or care how the blob was encrypted. It accepts the
already-canonical encrypted bytes and deterministically splits them.
"""

from __future__ import annotations

import hashlib

from .models import ChunkManifest, ChunkManifestEntry


def split_encrypted_blob(blob: bytes, chunk_size: int) -> list[tuple[int, int, bytes]]:
    """Split a canonical encrypted blob into deterministic fixed-size chunks.

    Returns a list of tuples: (offset, size, chunk_bytes).
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be > 0")

    chunks: list[tuple[int, int, bytes]] = []
    offset = 0
    while offset < len(blob):
        part = blob[offset: offset + chunk_size]
        chunks.append((offset, len(part), part))
        offset += len(part)
    return chunks


def build_chunk_manifest(
    *,
    board_id: str,
    post_id: str,
    thread_id: str,
    blob_kind: str,
    blob: bytes,
    chunk_size: int,
    manifest_version: int = 1,
    merkle_root: str | None = None,
) -> tuple[ChunkManifest, list[ChunkManifestEntry]]:
    """Build a structural manifest for a canonical encrypted blob.

    blob_hash remains SHA-256(blob), matching the existing canonical payload
    identity used everywhere else in the system.
    """
    blob_hash = hashlib.sha256(blob).hexdigest()
    chunks = split_encrypted_blob(blob, chunk_size)

    entries = [
        ChunkManifestEntry(
            blob_hash=blob_hash,
            chunk_index=index,
            offset=offset,
            size=size,
            chunk_hash=hashlib.sha256(chunk).hexdigest(),
        )
        for index, (offset, size, chunk) in enumerate(chunks)
    ]

    manifest = ChunkManifest(
        manifest_version=manifest_version,
        board_id=board_id,
        post_id=post_id,
        thread_id=thread_id,
        blob_kind=blob_kind,
        blob_hash=blob_hash,
        blob_size=len(blob),
        chunk_size=chunk_size,
        chunk_count=len(entries),
        merkle_root=merkle_root,
    )
    return manifest, entries
