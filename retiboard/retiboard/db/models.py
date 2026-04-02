"""
Data models for RetiBoard local storage.

Spec references:
    §3.1 — Structural metadata (plaintext). Strictly structural — never leaks content.
           Prohibited fields: no text previews, no image hints, no subjects, no filenames.
    §3.2 — Encrypted payload (opaque .bin blobs, never modeled here).
    §3.3 — Board announce schema.
    §4   — Pruning rules: expiry_timestamp, is_abandoned, thread_last_activity.

Design invariants:
    - ZERO content fields anywhere. If you're tempted to add a 'subject',
      'preview', 'filename', or 'snippet' field — DON'T. That violates §3.1.
    - expiry_timestamp tracks the thread TTL window shared by all posts in a thread.
    - thread_last_activity is denormalized onto the thread (OP row) for cheap pruning queries.
    - All fields are typed and documented. No untyped dicts flowing through the system.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


# =============================================================================
# Board model (§3.3 — Board Announce Schema)
# =============================================================================

@dataclass
class Board:
    """
    Local representation of a subscribed board.

    Mirrors the §3.3 board announce JSON schema. Stored in a global
    boards registry (or per-board meta.db).

    Note: key_material is intentionally stored here for passthrough to
    the frontend ONLY. The backend NEVER derives or uses the AES-GCM key.
    It is stored so the frontend can re-derive the board key per session (§5, §10).
    """

    board_id: str                       # Unique board identifier (hex hash)
    display_name: str                   # Human-readable board name
    text_only: bool = False             # §8.2: if True, attachment payloads forbidden
    default_ttl_seconds: int = 43_200   # §3.3: 12h thread start TTL
    bump_decay_rate: int = 3_600        # §3.3: per-bump thread TTL refill
    max_active_threads_local: int = 50  # §3.3: local thread cap
    pow_difficulty: int = 0             # §11: 0 = disabled
    key_material: str = ""              # §5: public, for frontend key derivation ONLY
    announce_version: int = 1           # Protocol version for this announce
    peer_lxmf_hash: str = ""            # v3.6.2 §4: node's LXMF delivery hash

    # Local-only fields (not part of the announce wire format):
    subscribed_at: float = field(default_factory=time.time)  # When we subscribed

    def to_announce_dict(self) -> dict:
        """
        Serialize to compact announce JSON for RNS broadcast.

        Uses short keys to fit within the RNS 500-byte MTU.
        The announce packet includes ~166 bytes of RNS overhead
        (signatures, hashes), leaving ~334 bytes for app_data.

        Key mapping:
            b  = board_id          n  = display_name
            to = text_only         ttl = default_ttl_seconds
            bdr = bump_decay_rate  mt = max_active_threads_local
            pow = pow_difficulty    km = key_material
            av = announce_version  plh = peer_lxmf_hash
        """
        d = {
            "b": self.board_id,
            "n": self.display_name,
            "km": self.key_material,
            "av": self.announce_version,
        }
        # Only include non-default values to save space.
        if self.text_only:
            d["to"] = True
        if self.default_ttl_seconds != 43_200:
            d["ttl"] = self.default_ttl_seconds
        if self.bump_decay_rate != 3_600:
            d["bdr"] = self.bump_decay_rate
        if self.max_active_threads_local != 50:
            d["mt"] = self.max_active_threads_local
        if self.pow_difficulty != 0:
            d["pow"] = self.pow_difficulty
        if self.peer_lxmf_hash:
            d["plh"] = self.peer_lxmf_hash
        return d

    @classmethod
    def from_announce_dict(cls, data: dict) -> Board:
        """
        Deserialize from announce JSON (supports both compact and verbose keys).

        Handles both compact (v3.6.2) and verbose (v3.6.1) formats.
        """
        # Detect format: compact uses "b" for board_id, verbose uses "board_id".
        if "b" in data:
            # Compact v3.6.2 format.
            return cls(
                board_id=data["b"],
                display_name=data.get("n", ""),
                text_only=data.get("to", False),
                default_ttl_seconds=data.get("ttl", 43_200),
                bump_decay_rate=data.get("bdr", 3_600),
                max_active_threads_local=data.get("mt", 50),
                pow_difficulty=data.get("pow", 0),
                key_material=data.get("km", ""),
                announce_version=data.get("av", 2),
                peer_lxmf_hash=data.get("plh", ""),
            )
        else:
            # Verbose v3.6.1 format (backward compat).
            return cls(
                board_id=data["board_id"],
                display_name=data.get("display_name", ""),
                text_only=data.get("text_only", False),
                default_ttl_seconds=data.get("default_ttl_seconds", 43_200),
                bump_decay_rate=data.get("bump_decay_rate", 3_600),
                max_active_threads_local=data.get("max_active_threads_local", 50),
                pow_difficulty=data.get("pow_difficulty", 0),
                key_material=data.get("key_material", ""),
                announce_version=data.get("announce_version", 1),
                peer_lxmf_hash=data.get("peer_lxmf_hash", ""),
            )


# =============================================================================
# Post metadata model (§3.1 — Structural Metadata)
# =============================================================================

@dataclass
class PostMetadata:
    """
    Structural metadata for a single post.

    This is the §3.1 plaintext record — transmitted, stored, indexed.
    Contains ZERO content: no text, no subjects, no filenames, no previews.

    Fields exactly mirror the spec JSON schema:
        post_id, thread_id, parent_id, timestamp, bump_flag, content_hash,
        payload_size, has_attachments, attachment_count, text_only, identity_hash, pow_nonce,
        thread_last_activity, is_abandoned

    Additional computed field:
        expiry_timestamp — current thread expiry shared by the whole thread.
                           Used by the pruner (§4) for thread lifecycle queries.
    """

    # === Core identifiers ===
    post_id: str                # Unique post ID (hex, typically hash-derived)
    thread_id: str              # Thread this post belongs to (= post_id for OPs)
    parent_id: str              # Direct parent (= "" for OPs, thread_id for flat replies)

    # === Timing ===
    timestamp: int              # Unix epoch seconds when created
    expiry_timestamp: int       # Current thread expiry shared across the thread (§4)

    # === Thread behavior ===
    bump_flag: bool             # If True, this post bumps the thread (sage = False)

    # === Payload reference ===
    content_hash: str           # SHA-256 hex of the encrypted TEXT payload
    payload_size: int           # Size of encrypted text .bin in bytes

    # === Attachment payload reference (split-blob model) ===
    attachment_content_hash: str = ""   # SHA-256 hex of encrypted attachment payload ("" = none)
    attachment_payload_size: int = 0    # Size of encrypted attachment .bin in bytes (0 = none)

    # === Content-type flags (structural ONLY — no actual content) ===
    has_attachments: bool = False      # True if encrypted payload contains attachments
    attachment_count: int = 0          # Declared number of attachments in the opaque attachment blob
    text_only: bool = False            # True if thread/board is text-only (§14.2)

    # === Identity (§12.3) ===
    identity_hash: str = ""     # Hash of poster's public key; "" for pure-anon

    # === Anti-spam (§11) ===
    pow_nonce: str = ""         # PoW solution nonce

    # === Private ping metadata (structural only; keys stay client-side) ===
    public_key: str = ""        # Ephemeral ECDH public key for replies
    encrypted_pings: list[str] = field(default_factory=list)  # Opaque ping blobs
    edit_signature: str = ""    # Reserved for future post editing

    # === Thread-level denormalized fields (on OP rows) ===
    thread_last_activity: int = 0   # Latest bump timestamp in this thread
    is_abandoned: bool = False      # §4: True when thread has expired and is pending purge

    @property
    def is_op(self) -> bool:
        """True if this post is the thread's original post (OP)."""
        return self.post_id == self.thread_id

    def to_dict(self) -> dict:
        """
        Serialize to the §3.1 metadata JSON (wire format).

        expiry_timestamp is local-only and excluded from the wire format
        (each node computes its own expiry based on local board config).
        """
        d = {
            "post_id": self.post_id,
            "thread_id": self.thread_id,
            "parent_id": self.parent_id,
            "timestamp": self.timestamp,
            "bump_flag": self.bump_flag,
            "content_hash": self.content_hash,
            "payload_size": self.payload_size,
            "attachment_content_hash": self.attachment_content_hash,
            "attachment_payload_size": self.attachment_payload_size,
            "has_attachments": self.has_attachments,
            "attachment_count": self.attachment_count,
            "text_only": self.text_only,
            "identity_hash": self.identity_hash,
            "pow_nonce": self.pow_nonce,
            "public_key": self.public_key,
            "encrypted_pings": list(self.encrypted_pings),
            "edit_signature": self.edit_signature,
            "thread_last_activity": self.thread_last_activity,
            "is_abandoned": self.is_abandoned,
        }
        return d

    @classmethod
    def from_dict(cls, data: dict, default_ttl: int = 43_200) -> PostMetadata:
        """
        Deserialize from §3.1 metadata JSON.

        Args:
            data: Metadata dict from wire or database.
            default_ttl: Board's TTL for computing expiry if not in data.
        """
        timestamp = data["timestamp"]
        raw_encrypted_pings = data.get("encrypted_pings", [])
        encrypted_pings = (
            [item for item in raw_encrypted_pings if isinstance(item, str)]
            if isinstance(raw_encrypted_pings, list)
            else []
        )
        return cls(
            post_id=data["post_id"],
            thread_id=data["thread_id"],
            parent_id=data.get("parent_id", ""),
            timestamp=timestamp,
            expiry_timestamp=data.get(
                "expiry_timestamp",
                timestamp + default_ttl,
            ),
            bump_flag=data.get("bump_flag", False),
            content_hash=data["content_hash"],
            payload_size=data.get("payload_size", 0),
            attachment_content_hash=data.get("attachment_content_hash", ""),
            attachment_payload_size=data.get("attachment_payload_size", 0),
            has_attachments=data.get("has_attachments", False),
            attachment_count=data.get("attachment_count", 0),
            text_only=data.get("text_only", False),
            identity_hash=data.get("identity_hash", ""),
            pow_nonce=data.get("pow_nonce", ""),
            public_key=data.get("public_key", ""),
            encrypted_pings=encrypted_pings,
            edit_signature=data.get("edit_signature", ""),
            thread_last_activity=data.get("thread_last_activity", timestamp),
            is_abandoned=data.get("is_abandoned", False),
        )


# =============================================================================
# Thread summary (convenience view, not a storage model)
# =============================================================================

@dataclass
class ThreadSummary:
    """
    Lightweight thread summary for catalog views and HAVE announcements.

    Not stored directly — derived from querying the posts table.
    Used by the frontend catalog and by Tier 2 HAVE packets (§7.1).
    """

    thread_id: str
    op_post_id: str                 # The OP's post_id (= thread_id)
    post_count: int                 # Total replies + OP
    latest_post_timestamp: int      # Most recent post timestamp
    thread_last_activity: int       # Last bump timestamp
    has_attachments: bool               # Whether OP has attachments
    text_only: bool                 # Thread-level text_only flag
    is_abandoned: bool              # Current abandonment state
    op_content_hash: str            # For fetching the OP text payload
    op_payload_size: int            # OP text payload size
    op_attachment_content_hash: str = "" # For fetching the OP attachment payload
    op_attachment_payload_size: int = 0  # OP attachment payload size
    op_attachment_count: int = 0         # Declared OP attachment count
    public_key: str = ""            # OP ephemeral ping public key
    op_identity_hash: str = ""      # OP identity hash for identity-based moderation
    expiry_timestamp: int = 0       # OP's expiry (determines thread expiry)
