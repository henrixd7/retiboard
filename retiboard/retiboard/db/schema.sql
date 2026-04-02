-- =============================================================================
-- RetiBoard v3.6.3 / Schema v14 — Local Storage Schema
-- =============================================================================
--
-- Spec references:
--   §3.1 — Structural metadata fields (ZERO content: no previews, subjects, filenames)
--   §3.3 — Board announce schema
--   §4   — Disk layout, pruning rules, expiry_timestamp, is_abandoned
--
-- Design:
--   Each board gets its OWN meta.db file at:
--       ~/.retiboard/boards/<board_id>/meta.db
--
--   This schema is applied per-board. The `boards` table exists as a
--   single-row self-description (the board's config). Cross-board queries
--   are never needed because boards are isolated by design (§4).
--
--   Delete board = rm -rf ~/.retiboard/boards/<board_id>/
--
-- Invariants enforced:
--   - No column holds content, text, filenames, or previews (§3.1 prohibited)
--   - expiry_timestamp is retained as structural lifecycle metadata
--   - thread_last_activity is denormalized on OP rows for cheap thread queries
--   - is_abandoned marks threads for full deletion (metadata + payloads)
-- =============================================================================

-- Schema version tracking for migrations.
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER NOT NULL DEFAULT 1,
    applied_at  INTEGER NOT NULL  -- Unix epoch
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Board configuration (§3.3)
-- Single row per database — this board's announce config.
--
-- NOTE: key_material is deliberately ABSENT from this table.
-- Per §5, key_material is stored only in the opaque announce.json cache
-- file and in-memory. The backend DB never persists it.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS board_config (
    board_id                    TEXT PRIMARY KEY,
    display_name                TEXT NOT NULL,
    text_only                   INTEGER NOT NULL DEFAULT 0,  -- boolean
    default_ttl_seconds         INTEGER NOT NULL DEFAULT 43200,
    bump_decay_rate             INTEGER NOT NULL DEFAULT 3600,
    max_active_threads_local    INTEGER NOT NULL DEFAULT 50,
    pow_difficulty              INTEGER NOT NULL DEFAULT 0,
    announce_version            INTEGER NOT NULL DEFAULT 1,
    subscribed_at               INTEGER NOT NULL  -- Unix epoch
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Posts (§3.1 structural metadata)
--
-- EVERY field here is structural metadata. ZERO content fields.
-- Prohibited by §3.1: text previews, image hints, subjects, filenames.
--
-- Design decisions:
--   • expiry_timestamp tracks the thread TTL window shared by every post row
--     in that thread for thread/blob lifecycle coordination
--   • thread_last_activity is meaningful on OP rows (where post_id = thread_id).
--     On reply rows it may be stale; the OP row is authoritative.
--   • is_abandoned is meaningful on OP rows only.
--     Set by pruner when the OP row's expiry_timestamp <= now.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS posts (
    -- Core identifiers
    post_id             TEXT PRIMARY KEY,
    thread_id           TEXT NOT NULL,
    parent_id           TEXT NOT NULL DEFAULT '',

    -- Timing
    timestamp           INTEGER NOT NULL,
    expiry_timestamp    INTEGER NOT NULL,  -- Shared thread expiry timestamp

    -- Thread behavior
    bump_flag           INTEGER NOT NULL DEFAULT 0,  -- boolean: 1 = bumps thread

    -- Payload reference (opaque — we never inspect the .bin)
    content_hash        TEXT NOT NULL,
    payload_size        INTEGER NOT NULL DEFAULT 0,

    -- Attachment payload reference (split-blob model)
    attachment_content_hash  TEXT NOT NULL DEFAULT '',
    attachment_payload_size  INTEGER NOT NULL DEFAULT 0,

    -- Content-type flags (structural ONLY)
    has_attachments           INTEGER NOT NULL DEFAULT 0,  -- boolean
    attachment_count          INTEGER NOT NULL DEFAULT 0,
    text_only           INTEGER NOT NULL DEFAULT 0,  -- boolean

    -- Identity (§12.3: hash of public key, not the key itself)
    identity_hash       TEXT NOT NULL DEFAULT '',

    -- Anti-spam (§11)
    pow_nonce           TEXT NOT NULL DEFAULT '',

    -- Ephemeral private ping metadata (structural only; no private keys)
    public_key          TEXT NOT NULL DEFAULT '',
    encrypted_pings     TEXT NOT NULL DEFAULT '[]',
    edit_signature      TEXT NOT NULL DEFAULT '',

    -- Thread-level denormalized (authoritative on OP rows only)
    thread_last_activity INTEGER NOT NULL DEFAULT 0,
    is_abandoned        INTEGER NOT NULL DEFAULT 0   -- boolean
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Indexes optimized for the operations we do most:
--   1. Pruning: find abandoned threads
--   2. Catalog: list active threads sorted by bump order
--   3. Thread view: list posts in a thread chronologically
--   4. Gossip: find threads by last_activity for HAVE announcements
-- ─────────────────────────────────────────────────────────────────────────────

-- Lifecycle metadata: supports thread/blob expiry lookups.
CREATE INDEX IF NOT EXISTS idx_posts_expiry
    ON posts (expiry_timestamp);

-- Pruning: find OP rows of abandoned threads for full deletion.
CREATE INDEX IF NOT EXISTS idx_posts_abandoned
    ON posts (is_abandoned, thread_id)
    WHERE is_abandoned = 1;

-- Catalog: active thread OPs sorted by bump order (thread_last_activity DESC).
-- Filtered to OP rows only (post_id = thread_id) and non-abandoned.
CREATE INDEX IF NOT EXISTS idx_thread_ops_active
    ON posts (thread_last_activity DESC)
    WHERE post_id = thread_id AND is_abandoned = 0;

-- Thread view: all posts in a given thread, chronological.
CREATE INDEX IF NOT EXISTS idx_posts_by_thread
    ON posts (thread_id, timestamp ASC);

-- Payload dedup: check if we already have a payload by content_hash.
CREATE INDEX IF NOT EXISTS idx_posts_content_hash
    ON posts (content_hash);

-- ─────────────────────────────────────────────────────────────────────────────
-- Thread summary view (convenience, not a physical table)
--
-- Derives catalog data from the posts table. Used for:
--   • Frontend catalog rendering (§10)
--   • HAVE announcement building (§7.1 Tier 2)
--
-- Only non-abandoned threads with OP rows are included.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE VIEW IF NOT EXISTS thread_summary AS
SELECT
    op.thread_id,
    op.post_id              AS op_post_id,
    op.thread_last_activity,
    op.has_attachments,
    op.text_only,
    op.is_abandoned,
    op.content_hash         AS op_content_hash,
    op.payload_size         AS op_payload_size,
    op.attachment_content_hash   AS op_attachment_content_hash,
    op.attachment_payload_size   AS op_attachment_payload_size,
    op.attachment_count     AS op_attachment_count,
    op.public_key,
    op.encrypted_pings,
    op.edit_signature,
    op.identity_hash        AS op_identity_hash,
    op.expiry_timestamp,
    -- Subquery for post count (OP + replies)
    (SELECT COUNT(*) FROM posts p WHERE p.thread_id = op.thread_id) AS post_count,
    -- Subquery for latest post timestamp in thread
    (SELECT MAX(p.timestamp) FROM posts p WHERE p.thread_id = op.thread_id) AS latest_post_timestamp
FROM posts op
WHERE op.post_id = op.thread_id       -- OP rows only
  AND op.is_abandoned = 0;             -- Exclude abandoned


-- ─────────────────────────────────────────────────────────────────────────────
-- Chunked transport metadata (Phase 1 foundation)
--
-- These tables are STRUCTURAL ONLY. They describe how to verify and reassemble
-- an already-encrypted canonical blob. They never contain plaintext content.
-- The canonical object identity remains SHA-256(encrypted_blob).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS chunk_manifests (
    blob_hash            TEXT PRIMARY KEY,
    board_id             TEXT NOT NULL,
    post_id              TEXT NOT NULL,
    thread_id            TEXT NOT NULL,
    blob_kind            TEXT NOT NULL CHECK(blob_kind IN ('text', 'attachments')),
    blob_size            INTEGER NOT NULL,
    chunk_size           INTEGER NOT NULL,
    chunk_count          INTEGER NOT NULL,
    merkle_root          TEXT,
    manifest_version     INTEGER NOT NULL DEFAULT 1,
    created_at           INTEGER NOT NULL,
    expires_at           INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS chunk_manifest_entries (
    blob_hash            TEXT NOT NULL,
    chunk_index          INTEGER NOT NULL,
    offset               INTEGER NOT NULL,
    size                 INTEGER NOT NULL,
    chunk_hash           TEXT NOT NULL,
    PRIMARY KEY (blob_hash, chunk_index),
    FOREIGN KEY (blob_hash) REFERENCES chunk_manifests(blob_hash) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS peer_chunk_availability (
    board_id            TEXT NOT NULL,
    peer_lxmf_hash      TEXT NOT NULL,
    blob_hash           TEXT NOT NULL,
    chunk_count         INTEGER NOT NULL,
    complete            INTEGER NOT NULL DEFAULT 0,
    ranges_json         TEXT NOT NULL DEFAULT "[]",
    last_seen_at        INTEGER NOT NULL,
    PRIMARY KEY (peer_lxmf_hash, blob_hash)
);

CREATE INDEX IF NOT EXISTS idx_peer_chunk_availability_blob
    ON peer_chunk_availability (board_id, blob_hash, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS chunk_fetch_sessions (
    session_id           TEXT PRIMARY KEY,
    board_id             TEXT NOT NULL,
    blob_hash            TEXT NOT NULL,
    blob_kind            TEXT NOT NULL CHECK(blob_kind IN ('text', 'attachments')),
    state                TEXT NOT NULL,
    request_peer_lxmf_hash TEXT NOT NULL DEFAULT '',
    started_at           INTEGER NOT NULL,
    updated_at           INTEGER NOT NULL,
    expires_at           INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_chunk_manifests_thread
    ON chunk_manifests (thread_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_chunk_sessions_blob
    ON chunk_fetch_sessions (blob_hash, updated_at DESC);

CREATE TABLE IF NOT EXISTS chunk_request_states (
    session_id           TEXT NOT NULL,
    chunk_index          INTEGER NOT NULL,
    state                TEXT NOT NULL,
    assigned_peer_lxmf_hash TEXT NOT NULL DEFAULT '',
    request_id           TEXT NOT NULL DEFAULT '',
    attempt_count        INTEGER NOT NULL DEFAULT 0,
    deadline_at          INTEGER NOT NULL DEFAULT 0,
    updated_at           INTEGER NOT NULL,
    PRIMARY KEY (session_id, chunk_index),
    FOREIGN KEY (session_id) REFERENCES chunk_fetch_sessions(session_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_chunk_request_states_session
    ON chunk_request_states (session_id, chunk_index);


CREATE TABLE IF NOT EXISTS chunk_peer_penalties (
    board_id                TEXT NOT NULL,
    peer_lxmf_hash          TEXT NOT NULL,
    timeout_count           INTEGER NOT NULL DEFAULT 0,
    invalid_chunk_count     INTEGER NOT NULL DEFAULT 0,
    success_count           INTEGER NOT NULL DEFAULT 0,
    cooldown_until          INTEGER NOT NULL DEFAULT 0,
    updated_at              INTEGER NOT NULL,
    PRIMARY KEY (board_id, peer_lxmf_hash)
);

CREATE INDEX IF NOT EXISTS idx_chunk_peer_penalties_board
    ON chunk_peer_penalties (board_id, cooldown_until DESC, updated_at DESC);


-- ─────────────────────────────────────────────────────────────────────────────
-- Local structural content control
--
-- Board-local moderation/retention decisions only. Structural identifiers only:
-- identity hash, thread id, post id. No plaintext content inspection.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS content_control (
    id              INTEGER PRIMARY KEY,
    scope           TEXT NOT NULL CHECK(scope IN ('identity', 'thread', 'post', 'attachment')),
    target_id       TEXT NOT NULL,
    action          TEXT NOT NULL CHECK(action IN ('block', 'hide', 'purge')),
    created_at      INTEGER NOT NULL,
    reason          TEXT NOT NULL DEFAULT '',
    is_active       INTEGER NOT NULL DEFAULT 1
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_content_control_unique
    ON content_control (scope, target_id, action);

CREATE INDEX IF NOT EXISTS idx_content_control_lookup
    ON content_control (scope, target_id, is_active);


-- ─────────────────────────────────────────────────────────────────────────────
-- Moderation action log
--
-- Tracks reversible cascades such as identity bans that purge existing
-- content. This lets us lift only the purge tombstones that were created by
-- a specific ban action when the ban is later reversed.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS moderation_actions (
    id              INTEGER PRIMARY KEY,
    action_kind     TEXT NOT NULL CHECK(action_kind IN ('identity_ban', 'attachment_ban')),
    target_id       TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    reason          TEXT NOT NULL DEFAULT '',
    is_active       INTEGER NOT NULL DEFAULT 1,
    reversed_at     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_moderation_actions_lookup
    ON moderation_actions (action_kind, target_id, is_active, created_at DESC);

CREATE TABLE IF NOT EXISTS moderation_action_targets (
    id              INTEGER PRIMARY KEY,
    action_id       INTEGER NOT NULL,
    target_scope    TEXT NOT NULL CHECK(target_scope IN ('thread', 'post')),
    target_id       TEXT NOT NULL,
    FOREIGN KEY (action_id) REFERENCES moderation_actions(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_moderation_action_targets_unique
    ON moderation_action_targets (action_id, target_scope, target_id);

CREATE INDEX IF NOT EXISTS idx_moderation_action_targets_lookup
    ON moderation_action_targets (action_id, target_scope, target_id);
