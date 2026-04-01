"""Structural control-plane messages for chunked payload transport.

Phase 1 adds the wire objects but does not yet replace the existing single-blob
PAYLOAD_REQUEST path. This keeps the architecture ready for the next step
without breaking current v3.6.2 behavior.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from retiboard.chunks.models import ChunkManifestEntry


@dataclass(frozen=True)
class ChunkManifestRequest:
    board_id: str
    blob_hash: str


@dataclass(frozen=True)
class ChunkManifestUnavailable:
    board_id: str
    blob_hash: str
    reason: Literal["abandoned", "pruned", "not_found", "policy_rejected", "withheld_local_policy"]


@dataclass(frozen=True)
class ChunkManifestResponse:
    board_id: str
    blob_hash: str
    blob_size: int
    chunk_size: int
    chunk_count: int
    merkle_root: str | None
    entries: list[ChunkManifestEntry]


@dataclass(frozen=True)
class ChunkRequest:
    board_id: str
    blob_hash: str
    chunk_index: int
    request_id: str


@dataclass(frozen=True)
class ChunkCancel:
    board_id: str
    blob_hash: str
    chunk_index: int
    request_id: str


@dataclass(frozen=True)
class ChunkOffer:
    board_id: str
    blob_hash: str
    chunk_count: int
    complete: bool
    ranges: list[tuple[int, int]]


@dataclass(frozen=True)
class ChunkDataEnvelope:
    """Metadata that accompanies a single chunk transfer on the data plane."""

    board_id: str
    blob_hash: str
    chunk_index: int
    request_id: str

    def to_dict(self) -> dict:
        return asdict(self)
