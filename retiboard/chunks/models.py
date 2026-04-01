from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, Optional

BlobKind = Literal["text", "attachments"]


class ChunkFetchState(str, Enum):
    """Single-chunk fetch lifecycle state.

    REQUEST_ENQUEUED is explicit because the existing v3.6.2 queue/path model
    is real transport state, not an implementation detail.
    """

    MISSING = "missing"
    SCHEDULED = "scheduled"
    REQUEST_ENQUEUED = "request_enqueued"
    REQUESTED = "requested"
    RECEIVED = "received"
    PREVALIDATED = "prevalidated"
    STORED = "stored"
    COMPLETE = "complete"
    FINALIZING = "finalizing"
    TIMED_OUT = "timed_out"
    REJECTED_INVALID = "rejected_invalid"
    CANCELLED = "cancelled"
    PEER_COOLDOWN = "peer_cooldown"


@dataclass(frozen=True)
class ChunkManifestEntry:
    """One verified structural description for a chunk of an encrypted blob."""

    blob_hash: str
    chunk_index: int
    offset: int
    size: int
    chunk_hash: str


@dataclass(frozen=True)
class ChunkManifest:
    """Structural-only manifest for one canonical encrypted blob.

    This contains no plaintext content, no filenames, and no MIME details.
    It only describes how to verify and reconstruct an already encrypted blob.
    """

    manifest_version: int
    board_id: str
    post_id: str
    thread_id: str
    blob_kind: BlobKind
    blob_hash: str
    blob_size: int
    chunk_size: int
    chunk_count: int
    merkle_root: Optional[str] = None
    created_at: int = field(default_factory=lambda: int(time.time()))


@dataclass
class ChunkFetchSession:
    """Minimal persisted state for a Phase 1 single-blob chunk fetch."""

    session_id: str
    board_id: str
    blob_hash: str
    blob_kind: BlobKind
    state: str
    started_at: int = field(default_factory=lambda: int(time.time()))
    updated_at: int = field(default_factory=lambda: int(time.time()))
    expires_at: int = 0
    request_peer_lxmf_hash: str = ""


@dataclass
class ChunkRequestStateRecord:
    """Persisted per-chunk fetch state for restart-safe resumption."""

    session_id: str
    chunk_index: int
    state: str
    assigned_peer_lxmf_hash: str = ""
    request_id: str = ""
    attempt_count: int = 0
    deadline_at: int = 0
    updated_at: int = field(default_factory=lambda: int(time.time()))


@dataclass
class ChunkPeerPenaltyRecord:
    """Persisted board-local peer cooldown / penalty state."""

    board_id: str
    peer_lxmf_hash: str
    timeout_count: int = 0
    invalid_chunk_count: int = 0
    success_count: int = 0
    cooldown_until: int = 0
    updated_at: int = field(default_factory=lambda: int(time.time()))
