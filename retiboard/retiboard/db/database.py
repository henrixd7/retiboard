"""
Async SQLite database wrapper for RetiBoard.

Spec references:
    §3.1 — Structural metadata storage (zero content)
    §3.3 — Board config storage
    §4   — Per-board disk layout: boards/<board_id>/meta.db
           Pruning: expiry_timestamp, is_abandoned, thread_last_activity

Design invariants:
    - Each board has its own SQLite file — total isolation.
    - Delete a board = rm -rf the board directory (§4).
    - All queries operate on structural metadata ONLY.
    - The database never sees, stores, or queries content/payloads.
    - thread_last_activity on OP rows is kept up-to-date on every bump.
    - expiry_timestamp is the shared thread expiry carried on every row
      in a thread so fetch/session coordination follows thread TTL exactly.

Thread lifecycle (§4):
    - New thread: expiry_timestamp = created_at + default_ttl_seconds
    - Bumping reply: expiry_timestamp += bump_decay_rate, capped at now + default_ttl_seconds
    - Expired thread: expiry_timestamp <= now → is_abandoned = True → fully deleted
"""

from __future__ import annotations

import time
import json
from pathlib import Path
from typing import Optional

import aiosqlite

from retiboard.config import BOARDS_DIR, DEFAULT_TTL_SECONDS, DEFAULT_BUMP_DECAY_RATE
from retiboard.db.models import Board, PostMetadata, ThreadSummary
from retiboard.chunks.models import (
    ChunkFetchSession,
    ChunkManifest,
    ChunkManifestEntry,
    ChunkPeerPenaltyRecord,
    ChunkRequestStateRecord,
)


# Current schema version. Bump this when schema.sql changes.
SCHEMA_VERSION = 14

# Load the schema SQL once at import time.
_SCHEMA_SQL = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")

_THREAD_SUMMARY_VIEW_SQL_V10 = """
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
        op.expiry_timestamp,
        (SELECT COUNT(*) FROM posts p WHERE p.thread_id = op.thread_id) AS post_count,
        (SELECT MAX(p.timestamp) FROM posts p WHERE p.thread_id = op.thread_id) AS latest_post_timestamp
    FROM posts op
    WHERE op.post_id = op.thread_id
      AND op.is_abandoned = 0
"""

_THREAD_SUMMARY_VIEW_SQL_V11 = """
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
        op.public_key,
        op.encrypted_pings,
        op.edit_signature,
        op.expiry_timestamp,
        (SELECT COUNT(*) FROM posts p WHERE p.thread_id = op.thread_id) AS post_count,
        (SELECT MAX(p.timestamp) FROM posts p WHERE p.thread_id = op.thread_id) AS latest_post_timestamp
    FROM posts op
    WHERE op.post_id = op.thread_id
      AND op.is_abandoned = 0
"""

_THREAD_SUMMARY_VIEW_SQL_V13 = """
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
        op.public_key,
        op.encrypted_pings,
        op.edit_signature,
        op.identity_hash        AS op_identity_hash,
        op.expiry_timestamp,
        (SELECT COUNT(*) FROM posts p WHERE p.thread_id = op.thread_id) AS post_count,
        (SELECT MAX(p.timestamp) FROM posts p WHERE p.thread_id = op.thread_id) AS latest_post_timestamp
    FROM posts op
    WHERE op.post_id = op.thread_id
      AND op.is_abandoned = 0
"""

_THREAD_SUMMARY_VIEW_SQL_V14 = """
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
        (SELECT COUNT(*) FROM posts p WHERE p.thread_id = op.thread_id) AS post_count,
        (SELECT MAX(p.timestamp) FROM posts p WHERE p.thread_id = op.thread_id) AS latest_post_timestamp
    FROM posts op
    WHERE op.post_id = op.thread_id
      AND op.is_abandoned = 0
"""


# =============================================================================
# Path helpers (§4 disk layout)
# =============================================================================

def board_dir(board_id: str) -> Path:
    """
    Return the root directory for a board's local storage.

    Layout per §4:
        ~/.retiboard/boards/<board_id>/
            meta.db
            payloads/<content_hash>.bin
    """
    return BOARDS_DIR / board_id


def board_db_path(board_id: str) -> Path:
    """Return the path to a board's SQLite database."""
    return board_dir(board_id) / "meta.db"


def board_payloads_dir(board_id: str) -> Path:
    """Return the path to a board's opaque payload directory."""
    return board_dir(board_id) / "payloads"


def board_chunk_cache_dir(board_id: str) -> Path:
    """Return the board-local ephemeral chunk cache directory."""
    return board_dir(board_id) / "chunk_cache"


def is_board_subscribed(board_id: str) -> bool:
    """
    Check if we are subscribed to a board (directory + meta.db exist).

    This is a read-only check that does NOT create any directories or
    files. Used by the HAVE handler to gate sync for boards we don't
    have locally, preventing ghost board resurrection when open_board_db
    would auto-create the directory structure.
    """
    return board_db_path(board_id).exists()


# =============================================================================
# Database connection & initialization
# =============================================================================

async def open_board_db(board_id: str) -> aiosqlite.Connection:
    """
    Open (or create) the SQLite database for a board.

    Creates the board directory and payloads subdirectory if needed.
    Applies the schema (CREATE IF NOT EXISTS is idempotent).
    Enables WAL mode for better concurrent read performance.

    Args:
        board_id: The board's unique identifier.

    Returns:
        An open aiosqlite connection. Caller is responsible for closing.
    """
    db_path = board_db_path(board_id)

    # Ensure directory tree exists (§4 layout).
    db_path.parent.mkdir(parents=True, exist_ok=True)
    board_payloads_dir(board_id).mkdir(parents=True, exist_ok=True)
    board_chunk_cache_dir(board_id).mkdir(parents=True, exist_ok=True)

    db = await aiosqlite.connect(str(db_path))
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA busy_timeout=5000")
    # Return rows as sqlite3.Row for dict-like access.
    db.row_factory = aiosqlite.Row

    # Apply schema (idempotent — all CREATE IF NOT EXISTS).
    await db.executescript(_SCHEMA_SQL)

    # Record schema version if not present.
    async with db.execute("SELECT COUNT(*) FROM schema_version") as cur:
        row = await cur.fetchone()
        if row[0] == 0:
            await db.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, int(time.time())),
            )
            await db.commit()

    # Run migrations for existing databases.
    await _migrate(db)

    return db


async def open_existing_board_db(board_id: str) -> aiosqlite.Connection:
    """
    Open the SQLite database for an already-subscribed board.

    Unlike open_board_db(), this helper is read/control safe: it refuses
    to create directories or initialize schema for unknown boards.

    Raises:
        FileNotFoundError: If the board is not subscribed locally.
    """
    if not is_board_subscribed(board_id):
        raise FileNotFoundError(f"Board {board_id} is not subscribed")
    return await open_board_db(board_id)


async def _recreate_thread_summary_view(
    db: aiosqlite.Connection,
    *,
    include_private_ping_fields: bool,
    include_identity_hash: bool = False,
    include_attachment_count: bool = False,
) -> None:
    """Recreate the thread_summary view for the requested schema level."""
    await db.execute("DROP VIEW IF EXISTS thread_summary")
    if include_attachment_count:
        sql = _THREAD_SUMMARY_VIEW_SQL_V14
    elif include_identity_hash:
        sql = _THREAD_SUMMARY_VIEW_SQL_V13
    elif include_private_ping_fields:
        sql = _THREAD_SUMMARY_VIEW_SQL_V11
    else:
        sql = _THREAD_SUMMARY_VIEW_SQL_V10
    await db.execute(sql)


async def _migrate(db: aiosqlite.Connection) -> None:
    """
    Run schema migrations for existing databases.

    v1 → v2: Add attachment_content_hash and attachment_payload_size columns
    for the split-blob payload model. Also recreate thread_summary
    view to include the new fields.

    v2 → v3: Add structural chunk transport tables used by the
    Phase 1 multi-chunk fetch foundation.

    v3 → v4: Add persisted per-chunk request state for restart-safe
    chunk session resumption and explicit cancel handling.

    v10 → v11: Add structural metadata for ephemeral private pings and
    future post editing signatures.

    v11 → v12: Rename attachment-related structural columns and reset
    ephemeral chunk state so blob_kind can move from "media" to
    "attachments" without stale constraints.

    v13 → v14: Add structural attachment_count metadata and expose it
    on thread summaries.
    """
    async with db.execute(
        "SELECT MAX(version) FROM schema_version"
    ) as cur:
        row = await cur.fetchone()
        current_version = row[0] if row and row[0] else 1

    if current_version < 2:
        # Add split-blob columns (safe to run multiple times with IF NOT EXISTS
        # pattern via try/except — SQLite doesn't support IF NOT EXISTS for
        # ALTER TABLE ADD COLUMN).
        for col, typedef in [
            ("attachment_content_hash", "TEXT NOT NULL DEFAULT ''"),
            ("attachment_payload_size", "INTEGER NOT NULL DEFAULT 0"),
        ]:
            try:
                await db.execute(f"ALTER TABLE posts ADD COLUMN {col} {typedef}")
            except Exception:
                pass  # Column already exists.

        # Recreate the thread_summary view with new columns.
        await _recreate_thread_summary_view(db, include_private_ping_fields=False)

        await db.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (2, int(time.time())),
        )
        await db.commit()

    if current_version < 3:
        await db.executescript(
            """
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
            """
        )
        await db.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (3, int(time.time())),
        )
        await db.commit()

    if current_version < 4:
        await db.executescript(
            """
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
            """
        )
        await db.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (4, int(time.time())),
        )
        await db.commit()

    if current_version < 12:
        async with db.execute("PRAGMA table_info(posts)") as cur:
            post_columns = {row["name"] for row in await cur.fetchall()}

        for old_name, new_name in [
            ("media_content_hash", "attachment_content_hash"),
            ("media_payload_size", "attachment_payload_size"),
            ("has_media", "has_attachments"),
        ]:
            if old_name in post_columns and new_name not in post_columns:
                await db.execute(
                    f"ALTER TABLE posts RENAME COLUMN {old_name} TO {new_name}"
                )

        await db.executescript(
            """
            DROP VIEW IF EXISTS thread_summary;
            DROP TABLE IF EXISTS chunk_request_states;
            DROP TABLE IF EXISTS chunk_fetch_sessions;
            DROP TABLE IF EXISTS chunk_manifest_entries;
            DROP TABLE IF EXISTS chunk_manifests;
            """
        )
        await db.executescript(_SCHEMA_SQL)
        await _recreate_thread_summary_view(db, include_private_ping_fields=True)
        await db.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (12, int(time.time())),
        )
        await db.commit()

    if current_version < 5:
        await db.executescript(
            """
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
            """
        )
        await db.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (5, int(time.time())),
        )
        await db.commit()

    if current_version < 6:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS content_control (
                id              INTEGER PRIMARY KEY,
                scope           TEXT NOT NULL CHECK(scope IN ('identity', 'thread', 'post')),
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
            """
        )
        await db.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (6, int(time.time())),
        )
        await db.commit()

    if current_version < 7:
        await db.execute(
            """
            UPDATE board_config
            SET bump_decay_rate = 21600
            WHERE bump_decay_rate = 3600
            """
        )
        await db.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (7, int(time.time())),
        )
        await db.commit()

    if current_version < 8:
        await db.execute(
            """
            UPDATE board_config
            SET default_ttl_seconds = 43200,
                bump_decay_rate = 3600
            WHERE (default_ttl_seconds = 172800 AND bump_decay_rate = 21600)
               OR (default_ttl_seconds = 172800 AND bump_decay_rate = 3600)
            """
        )
        await db.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (8, int(time.time())),
        )
        await db.commit()

    if current_version < 9:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS moderation_actions (
                id              INTEGER PRIMARY KEY,
                action_kind     TEXT NOT NULL CHECK(action_kind IN ('identity_ban')),
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
            """
        )
        await db.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (9, int(time.time())),
        )
        await db.commit()

    if current_version < 10:
        await _recreate_thread_summary_view(db, include_private_ping_fields=False)
        await db.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (10, int(time.time())),
        )
        await db.commit()

    if current_version < 11:
        for col, typedef in [
            ("public_key", "TEXT NOT NULL DEFAULT ''"),
            ("encrypted_pings", "TEXT NOT NULL DEFAULT '[]'"),
            ("edit_signature", "TEXT NOT NULL DEFAULT ''"),
        ]:
            try:
                await db.execute(f"ALTER TABLE posts ADD COLUMN {col} {typedef}")
            except Exception:
                pass

        await _recreate_thread_summary_view(db, include_private_ping_fields=True)
        await db.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (11, int(time.time())),
        )
        await db.commit()

    if current_version < 13:
        # ── Widen content_control.scope CHECK to include 'attachment' ────
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS content_control_new (
                id              INTEGER PRIMARY KEY,
                scope           TEXT NOT NULL CHECK(scope IN ('identity', 'thread', 'post', 'attachment')),
                target_id       TEXT NOT NULL,
                action          TEXT NOT NULL CHECK(action IN ('block', 'hide', 'purge')),
                created_at      INTEGER NOT NULL,
                reason          TEXT NOT NULL DEFAULT '',
                is_active       INTEGER NOT NULL DEFAULT 1
            );
            INSERT OR IGNORE INTO content_control_new
                SELECT id, scope, target_id, action, created_at, reason, is_active
                FROM content_control;
            DROP TABLE IF EXISTS content_control;
            ALTER TABLE content_control_new RENAME TO content_control;

            CREATE UNIQUE INDEX IF NOT EXISTS idx_content_control_unique
                ON content_control (scope, target_id, action);
            CREATE INDEX IF NOT EXISTS idx_content_control_lookup
                ON content_control (scope, target_id, is_active);
            """
        )

        # ── Widen moderation_actions.action_kind CHECK ──────────────────
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS moderation_actions_new (
                id              INTEGER PRIMARY KEY,
                action_kind     TEXT NOT NULL CHECK(action_kind IN ('identity_ban', 'attachment_ban')),
                target_id       TEXT NOT NULL,
                created_at      INTEGER NOT NULL,
                reason          TEXT NOT NULL DEFAULT '',
                is_active       INTEGER NOT NULL DEFAULT 1,
                reversed_at     INTEGER NOT NULL DEFAULT 0
            );
            INSERT OR IGNORE INTO moderation_actions_new
                SELECT id, action_kind, target_id, created_at, reason, is_active, reversed_at
                FROM moderation_actions;
            DROP TABLE IF EXISTS moderation_actions;
            ALTER TABLE moderation_actions_new RENAME TO moderation_actions;

            CREATE INDEX IF NOT EXISTS idx_moderation_actions_lookup
                ON moderation_actions (action_kind, target_id, is_active, created_at DESC);
            """
        )

        # ── Add index for efficient identity-based lookups ──────────────
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_posts_identity ON posts (identity_hash)"
        )

        # ── Recreate thread_summary view with op_identity_hash ──────────
        await _recreate_thread_summary_view(
            db, include_private_ping_fields=True, include_identity_hash=True,
        )

        await db.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (13, int(time.time())),
        )
        await db.commit()

    if current_version < 14:
        try:
            await db.execute(
                "ALTER TABLE posts ADD COLUMN attachment_count INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass

        await db.execute(
            """
            UPDATE posts
            SET attachment_count = CASE
                WHEN has_attachments = 1 THEN 1
                ELSE 0
            END
            WHERE attachment_count = 0
            """
        )

        await _recreate_thread_summary_view(
            db,
            include_private_ping_fields=True,
            include_identity_hash=True,
            include_attachment_count=True,
        )
        await db.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (14, int(time.time())),
        )
        await db.commit()


# =============================================================================
# Board config CRUD (§3.3)
# =============================================================================

async def save_board_config(db: aiosqlite.Connection, board: Board) -> None:
    """
    Insert or replace the board configuration.

    Each per-board meta.db has exactly one row in board_config.
    This is called when subscribing to a board or updating its announce.

    NOTE: key_material is deliberately NOT stored here (§5).
    The board passed in should already have key_material="" via
    board_for_db_storage(). We enforce this by simply not including
    it in the SQL.
    """
    await db.execute(
        """
        INSERT OR REPLACE INTO board_config (
            board_id, display_name, text_only, default_ttl_seconds,
            bump_decay_rate, max_active_threads_local, pow_difficulty,
            announce_version, subscribed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            board.board_id,
            board.display_name,
            int(board.text_only),
            board.default_ttl_seconds,
            board.bump_decay_rate,
            board.max_active_threads_local,
            board.pow_difficulty,
            board.announce_version,
            int(board.subscribed_at),
        ),
    )
    await db.commit()


async def load_board_config(db: aiosqlite.Connection) -> Optional[Board]:
    """
    Load the board configuration from this database.

    Returns None if no config exists (shouldn't happen after proper init).

    NOTE: key_material is NOT in the database (§5). The returned Board
    will have key_material="". The caller (BoardManager) is responsible
    for attaching key_material from the in-memory/disk cache.
    """
    async with db.execute("SELECT * FROM board_config LIMIT 1") as cur:
        row = await cur.fetchone()
        if row is None:
            return None
        return Board(
            board_id=row["board_id"],
            display_name=row["display_name"],
            text_only=bool(row["text_only"]),
            default_ttl_seconds=row["default_ttl_seconds"],
            bump_decay_rate=row["bump_decay_rate"],
            max_active_threads_local=row["max_active_threads_local"],
            pow_difficulty=row["pow_difficulty"],
            key_material="",  # Never in DB — attached by BoardManager
            announce_version=row["announce_version"],
            subscribed_at=float(row["subscribed_at"]),
        )


# =============================================================================
# Post CRUD (§3.1)
# =============================================================================

async def insert_post(
    db: aiosqlite.Connection,
    post: PostMetadata,
    thread_start_ttl: Optional[int] = None,
    thread_bump_ttl: Optional[int] = None,
    *,
    commit: bool = True,
) -> None:
    """
    Insert a post's structural metadata into the database.

    Recomputes the thread lifecycle after insertion so the OP row and all
    thread rows share the same expiry_timestamp.

    Args:
        db: Open database connection for the board.
        post: The post metadata to insert.
        thread_start_ttl: Starting thread TTL in seconds. If omitted,
            uses the board's stored config.
        thread_bump_ttl: Per-bump thread TTL refill in seconds. If omitted,
            uses the board's stored config.

    Raises:
        aiosqlite.IntegrityError: If post_id already exists (duplicate).
    """
    if thread_start_ttl is None or thread_bump_ttl is None:
        config = await load_board_config(db)
        if config is not None:
            if thread_start_ttl is None:
                thread_start_ttl = int(config.default_ttl_seconds)
            if thread_bump_ttl is None:
                thread_bump_ttl = int(config.bump_decay_rate)

    if thread_start_ttl is None:
        thread_start_ttl = DEFAULT_TTL_SECONDS
    if thread_bump_ttl is None:
        thread_bump_ttl = DEFAULT_BUMP_DECAY_RATE

    await db.execute(
        """
        INSERT INTO posts (
            post_id, thread_id, parent_id, timestamp, expiry_timestamp,
            bump_flag, content_hash, payload_size,
            attachment_content_hash, attachment_payload_size,
            has_attachments, attachment_count, text_only,
            identity_hash, pow_nonce, public_key, encrypted_pings,
            edit_signature, thread_last_activity, is_abandoned
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            post.post_id,
            post.thread_id,
            post.parent_id,
            post.timestamp,
            post.expiry_timestamp,
            int(post.bump_flag),
            post.content_hash,
            post.payload_size,
            post.attachment_content_hash,
            post.attachment_payload_size,
            int(post.has_attachments),
            post.attachment_count,
            int(post.text_only),
            post.identity_hash,
            post.pow_nonce,
            post.public_key,
            json.dumps(post.encrypted_pings, separators=(",", ":")),
            post.edit_signature,
            post.thread_last_activity,
            int(post.is_abandoned),
        ),
    )

    await _recompute_thread_lifecycle(
        db,
        post.thread_id,
        thread_start_ttl=thread_start_ttl,
        thread_bump_ttl=thread_bump_ttl,
    )

    async with db.execute(
        """
        SELECT expiry_timestamp, thread_last_activity, is_abandoned
        FROM posts
        WHERE post_id = ?
        LIMIT 1
        """,
        (post.post_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is not None:
        post.expiry_timestamp = int(row["expiry_timestamp"])
        post.thread_last_activity = int(row["thread_last_activity"])
        post.is_abandoned = bool(row["is_abandoned"])

    if commit:
        await db.commit()


def _extend_thread_expiry(
    current_expiry: int,
    *,
    event_timestamp: int,
    thread_start_ttl: int,
    thread_bump_ttl: int,
) -> int:
    """
    Apply one capped TTL refill for a bump event.

    The refill never lets the thread have more than thread_start_ttl seconds
    remaining from the time of the bump.
    """
    return min(
        max(current_expiry, event_timestamp) + thread_bump_ttl,
        event_timestamp + thread_start_ttl,
    )


async def _recompute_thread_lifecycle(
    db: aiosqlite.Connection,
    thread_id: str,
    *,
    thread_start_ttl: int,
    thread_bump_ttl: int,
) -> None:
    """
    Recompute the authoritative thread lifecycle from the stored post sequence.

    This keeps the capped thread TTL deterministic even when replies arrive
    before the OP during sync. Once the OP exists locally, the full thread is
    replayed in timestamp order to derive:

    - OP thread_last_activity
    - shared thread expiry_timestamp for every row in the thread
    """
    async with db.execute(
        """
        SELECT post_id, timestamp, bump_flag
        FROM posts
        WHERE thread_id = ?
        ORDER BY timestamp ASC, post_id ASC
        """,
        (thread_id,),
    ) as cur:
        rows = await cur.fetchall()

    op_row = next((row for row in rows if row["post_id"] == thread_id), None)
    if op_row is None:
        return

    op_timestamp = int(op_row["timestamp"])
    thread_last_activity = op_timestamp
    thread_expiry = op_timestamp + thread_start_ttl

    for row in rows:
        if row["post_id"] == thread_id:
            continue
        if not bool(row["bump_flag"]):
            continue

        event_timestamp = max(int(row["timestamp"]), op_timestamp)
        thread_last_activity = max(thread_last_activity, event_timestamp)
        thread_expiry = _extend_thread_expiry(
            thread_expiry,
            event_timestamp=event_timestamp,
            thread_start_ttl=thread_start_ttl,
            thread_bump_ttl=thread_bump_ttl,
        )

    await db.execute(
        """
        UPDATE posts
        SET expiry_timestamp = ?
        WHERE thread_id = ?
        """,
        (thread_expiry, thread_id),
    )
    await db.execute(
        """
        UPDATE posts
        SET thread_last_activity = ?
        WHERE post_id = thread_id
          AND thread_id = ?
        """,
        (thread_last_activity, thread_id),
    )


async def recompute_thread_lifecycle(
    db: aiosqlite.Connection,
    thread_id: str,
) -> None:
    """
    Recompute one thread's shared lifecycle fields using the stored board config.

    This is used after local moderation deletes one or more reply rows so the
    remaining thread keeps a correct OP bump timestamp and shared expiry.
    """
    config = await load_board_config(db)
    thread_start_ttl = int(config.default_ttl_seconds) if config is not None else DEFAULT_TTL_SECONDS
    thread_bump_ttl = int(config.bump_decay_rate) if config is not None else DEFAULT_BUMP_DECAY_RATE
    await _recompute_thread_lifecycle(
        db,
        thread_id,
        thread_start_ttl=thread_start_ttl,
        thread_bump_ttl=thread_bump_ttl,
    )


async def get_post(
    db: aiosqlite.Connection,
    post_id: str,
) -> Optional[PostMetadata]:
    """Fetch a single post by ID. Returns None if not found."""
    async with db.execute(
        "SELECT * FROM posts WHERE post_id = ?", (post_id,)
    ) as cur:
        row = await cur.fetchone()
        if row is None:
            return None
        return _row_to_post(row)


async def get_thread_posts(
    db: aiosqlite.Connection,
    thread_id: str,
) -> list[PostMetadata]:
    """
    Fetch all posts in a thread, ordered chronologically.

    Returns empty list if thread doesn't exist or is expired/abandoned.
    """
    async with db.execute(
        """
        SELECT * FROM posts
        WHERE thread_id = ?
        ORDER BY timestamp ASC
        """,
        (thread_id,),
    ) as cur:
        rows = await cur.fetchall()
        return [_row_to_post(row) for row in rows]


async def post_exists(db: aiosqlite.Connection, post_id: str) -> bool:
    """Check if a post already exists (for dedup during gossip)."""
    async with db.execute(
        "SELECT 1 FROM posts WHERE post_id = ? LIMIT 1", (post_id,)
    ) as cur:
        return (await cur.fetchone()) is not None


async def content_hash_exists(
    db: aiosqlite.Connection,
    content_hash: str,
) -> bool:
    """Check if any post references this content_hash (payload dedup)."""
    async with db.execute(
        "SELECT 1 FROM posts WHERE content_hash = ? LIMIT 1",
        (content_hash,),
    ) as cur:
        return (await cur.fetchone()) is not None


# =============================================================================
# Thread / catalog queries
# =============================================================================

async def get_catalog(
    db: aiosqlite.Connection,
    limit: int = 50,
) -> list[ThreadSummary]:
    """
    Fetch the board catalog: active threads sorted by bump order.

    Uses the thread_summary view which filters to non-abandoned OP rows.

    Args:
        limit: Maximum threads to return (default matches max_active_threads_local).

    Returns:
        List of ThreadSummary objects, newest bump first.
    """
    async with db.execute(
        """
        SELECT * FROM thread_summary
        ORDER BY thread_last_activity DESC
        LIMIT ?
        """,
        (limit,),
    ) as cur:
        rows = await cur.fetchall()
        return [
            ThreadSummary(
                thread_id=row["thread_id"],
                op_post_id=row["op_post_id"],
                post_count=row["post_count"],
                latest_post_timestamp=row["latest_post_timestamp"],
                thread_last_activity=row["thread_last_activity"],
                has_attachments=bool(row["has_attachments"]),
                text_only=bool(row["text_only"]),
                is_abandoned=bool(row["is_abandoned"]),
                op_content_hash=row["op_content_hash"],
                op_payload_size=row["op_payload_size"],
                op_attachment_content_hash=row["op_attachment_content_hash"] if "op_attachment_content_hash" in row.keys() else "",
                op_attachment_payload_size=row["op_attachment_payload_size"] if "op_attachment_payload_size" in row.keys() else 0,
                op_attachment_count=row["op_attachment_count"] if "op_attachment_count" in row.keys() else 0,
                public_key=row["public_key"] if "public_key" in row.keys() else "",
                op_identity_hash=row["op_identity_hash"] if "op_identity_hash" in row.keys() else "",
                expiry_timestamp=row["expiry_timestamp"],
            )
            for row in rows
        ]


async def get_thread_count(db: aiosqlite.Connection) -> int:
    """Count of active (non-abandoned) threads."""
    async with db.execute(
        """
        SELECT COUNT(*) FROM posts
        WHERE post_id = thread_id AND is_abandoned = 0
        """
    ) as cur:
        row = await cur.fetchone()
        return row[0]


# =============================================================================
# Pruning queries (§4)
#
# These are the building blocks for the Phase 4 pruner. Included here
# because they're tightly coupled to the schema design.
# =============================================================================

async def mark_expired_threads(
    db: aiosqlite.Connection,
    now: Optional[int] = None,
    pinned_thread_ids: Optional[set[str]] = None,
) -> list[str]:
    """
    Mark expired threads for deletion.

    A thread is expired when its authoritative OP expiry_timestamp is at or
    before now. The pruner then deletes the whole thread in the same cycle.

    Args:
        now: Current unix timestamp (injectable for testing).

    Returns:
        List of thread_ids that were newly marked expired.
    """
    if now is None:
        now = int(time.time())
    pinned_thread_ids = pinned_thread_ids or set()

    # Find threads whose thread TTL has expired.
    async with db.execute(
        """
        SELECT thread_id FROM posts
        WHERE post_id = thread_id
          AND is_abandoned = 0
          AND expiry_timestamp <= ?
        """,
        (now,),
    ) as cur:
        rows = await cur.fetchall()
        expired_ids = [
            row["thread_id"]
            for row in rows
            if row["thread_id"] not in pinned_thread_ids
        ]

    if expired_ids:
        # Mark the OP rows as expired/pending purge.
        placeholders = ",".join("?" for _ in expired_ids)
        await db.execute(
            f"""
            UPDATE posts SET is_abandoned = 1
            WHERE post_id = thread_id
              AND thread_id IN ({placeholders})
            """,
            expired_ids,
        )
        await db.commit()

    return expired_ids


async def delete_abandoned_threads(
    db: aiosqlite.Connection,
) -> list[tuple[str, list[str]]]:
    """
    Delete all metadata for abandoned threads.

    Per §4: "abandoned threads are purged entirely (no stubs)."
    Returns list of (thread_id, [content_hashes]) so the caller can
    also delete the corresponding payload files.

    This is the metadata side of the deletion. The caller (pruner)
    must also call payload storage to remove the .bin files.
    """
    # First, collect content hashes for payload cleanup.
    results: list[tuple[str, list[str]]] = []

    async with db.execute(
        """
        SELECT DISTINCT thread_id FROM posts
        WHERE is_abandoned = 1
          AND post_id = thread_id
        """
    ) as cur:
        thread_rows = await cur.fetchall()

    for trow in thread_rows:
        tid = trow["thread_id"]
        async with db.execute(
            "SELECT content_hash, attachment_content_hash FROM posts WHERE thread_id = ?",
            (tid,),
        ) as cur:
            hash_rows = await cur.fetchall()
            hashes = [r["content_hash"] for r in hash_rows]
            # Also collect attachment blob hashes for deletion.
            for r in hash_rows:
                mch = r["attachment_content_hash"] if "attachment_content_hash" in r.keys() else ""
                if mch:
                    hashes.append(mch)
        results.append((tid, hashes))

    # Delete all posts in abandoned threads (OP + replies).
    if results:
        all_thread_ids = [tid for tid, _ in results]
        await delete_threads_bulk(db, all_thread_ids)

    return results


async def delete_threads_bulk(db: aiosqlite.Connection, thread_ids: list[str]) -> None:
    """
    Delete multiple threads (metadata only) in a single transaction.
    """
    if not thread_ids:
        return

    # Delete all posts in these threads
    placeholders = ",".join(["?"] * len(thread_ids))
    await db.execute(
        f"DELETE FROM posts WHERE thread_id IN ({placeholders})",
        thread_ids
    )
    await db.commit()


async def get_all_active_threads_global(
    pinned_thread_keys: Optional[set[str]] = None,
) -> list[tuple[str, str, list[str], int, int]]:
    """
    Fetch all active thread OPs across all boards, sorted by oldest activity.

    Returns:
        List of (board_id, thread_id, content_hashes, total_size_bytes, last_activity)
    """
    from retiboard.config import BOARDS_DIR
    all_threads = []
    pinned_thread_keys = pinned_thread_keys or set()

    for entry in sorted(BOARDS_DIR.iterdir()):
        if not entry.is_dir():
            continue
        meta_path = entry / "meta.db"
        if not meta_path.exists():
            continue

        board_id = entry.name
        db = await open_board_db(board_id)
        try:
            # Get OPs and their total payload sizes (sum of text + attachments for all posts in thread)
            async with db.execute(
                """
                SELECT
                    op.thread_id,
                    (SELECT SUM(payload_size + attachment_payload_size) 
                     FROM posts WHERE thread_id = op.thread_id) as total_size,
                    op.thread_last_activity
                FROM posts op
                WHERE op.post_id = op.thread_id AND op.is_abandoned = 0
                """
            ) as cursor:
                async for row in cursor:
                    thread_id, total_size, last_activity = row
                    if f"{board_id}:{thread_id}" in pinned_thread_keys:
                        continue
                    
                    # Fetch all content hashes for this thread to delete payloads later
                    hashes = []
                    async with db.execute(
                        "SELECT content_hash, attachment_content_hash FROM posts WHERE thread_id = ?",
                        (thread_id,)
                    ) as h_cursor:
                        async for h_row in h_cursor:
                            if h_row["content_hash"]:
                                hashes.append(h_row["content_hash"])
                            if h_row["attachment_content_hash"]:
                                hashes.append(h_row["attachment_content_hash"])
                    
                    all_threads.append((
                        board_id,
                        thread_id,
                        hashes,
                        total_size or 0,
                        last_activity
                    ))
        finally:
            await db.close()

    # Sort by last_activity (oldest first)
    all_threads.sort(key=lambda x: x[4])
    return all_threads


async def enforce_thread_cap(
    db: aiosqlite.Connection,
    max_threads: int,
    pinned_thread_ids: Optional[set[str]] = None,
) -> list[tuple[str, list[str]]]:
    """
    Enforce max_active_threads_local cap (§3.3).

    If active thread count exceeds the cap, the oldest threads (by
    thread_last_activity) are marked abandoned and deleted.

    Returns list of (thread_id, [content_hashes]) for payload cleanup.
    """
    pinned_thread_ids = pinned_thread_ids or set()

    async with db.execute(
        """
        SELECT thread_id FROM posts
        WHERE post_id = thread_id AND is_abandoned = 0
        ORDER BY thread_last_activity ASC
        """
    ) as cur:
        rows = await cur.fetchall()
        active_thread_ids = [row["thread_id"] for row in rows]

    unpinned_thread_ids = [
        thread_id for thread_id in active_thread_ids
        if thread_id not in pinned_thread_ids
    ]
    if len(active_thread_ids) <= max_threads or len(unpinned_thread_ids) <= 0:
        return []

    excess = len(active_thread_ids) - max_threads
    cull_ids = unpinned_thread_ids[:excess]

    if not cull_ids:
        return []

    # Mark them expired, then delete.
    placeholders = ",".join("?" for _ in cull_ids)
    await db.execute(
        f"""
        UPDATE posts SET is_abandoned = 1
        WHERE post_id = thread_id AND thread_id IN ({placeholders})
        """,
        cull_ids,
    )
    await db.commit()

    return await delete_abandoned_threads(db)




async def get_blob_reference(
    db: aiosqlite.Connection,
    blob_hash: str,
) -> Optional[dict]:
    """Resolve one canonical blob hash to structural post/thread context.

    Returns a dict with post_id, thread_id, blob_kind, expiry_timestamp,
    and text_only flag, or None if the hash is unknown on this board.
    """
    async with db.execute(
        """
        SELECT post_id, thread_id, expiry_timestamp, text_only, identity_hash
        FROM posts
        WHERE content_hash = ?
        LIMIT 1
        """,
        (blob_hash,),
    ) as cur:
        row = await cur.fetchone()
    if row is not None:
        return {
            "post_id": row["post_id"],
            "thread_id": row["thread_id"],
            "blob_kind": "text",
            "expiry_timestamp": int(row["expiry_timestamp"]),
            "text_only": bool(row["text_only"]),
            "identity_hash": row["identity_hash"],
        }

    async with db.execute(
        """
        SELECT post_id, thread_id, expiry_timestamp, text_only, identity_hash
        FROM posts
        WHERE attachment_content_hash = ?
        LIMIT 1
        """,
        (blob_hash,),
    ) as cur:
        row = await cur.fetchone()
    if row is not None:
        return {
            "post_id": row["post_id"],
            "thread_id": row["thread_id"],
            "blob_kind": "attachments",
            "expiry_timestamp": int(row["expiry_timestamp"]),
            "text_only": bool(row["text_only"]),
            "identity_hash": row["identity_hash"],
        }

    return None
async def get_declared_payload_size(
    db: aiosqlite.Connection,
    content_hash: str,
) -> Optional[int]:
    """Return the declared payload size for a text or attachment blob hash."""
    async with db.execute(
        """
        SELECT payload_size AS size
        FROM posts
        WHERE content_hash = ?
        LIMIT 1
        """,
        (content_hash,),
    ) as cur:
        row = await cur.fetchone()
    if row is not None:
        return int(row["size"])

    async with db.execute(
        """
        SELECT attachment_payload_size AS size
        FROM posts
        WHERE attachment_content_hash = ?
        LIMIT 1
        """,
        (content_hash,),
    ) as cur:
        row = await cur.fetchone()
    if row is not None:
        return int(row["size"])

    return None


CONTROL_SCOPE_IDENTITY = "identity"
CONTROL_SCOPE_THREAD = "thread"
CONTROL_SCOPE_POST = "post"
CONTROL_SCOPE_ATTACHMENT = "attachment"

CONTROL_ACTION_BLOCK = "block"
CONTROL_ACTION_HIDE = "hide"
CONTROL_ACTION_PURGE = "purge"


async def set_control(
    db: aiosqlite.Connection,
    *,
    scope: str,
    target_id: str,
    action: str,
    reason: str = "",
) -> None:
    """Create or reactivate one local structural content-control rule."""
    await db.execute(
        """
        INSERT INTO content_control (scope, target_id, action, created_at, reason, is_active)
        VALUES (?, ?, ?, ?, ?, 1)
        ON CONFLICT(scope, target_id, action) DO UPDATE SET
            created_at = excluded.created_at,
            reason = excluded.reason,
            is_active = 1
        """,
        (scope, target_id, action, int(time.time()), reason),
    )
    await db.commit()


async def clear_control(
    db: aiosqlite.Connection,
    *,
    scope: str,
    target_id: str,
    action: str,
) -> None:
    """Deactivate one local structural content-control rule."""
    await db.execute(
        """
        UPDATE content_control
        SET is_active = 0
        WHERE scope = ? AND target_id = ? AND action = ?
        """,
        (scope, target_id, action),
    )
    await db.commit()


async def has_control(
    db: aiosqlite.Connection,
    *,
    scope: str,
    target_id: str,
    action: str,
) -> bool:
    async with db.execute(
        """
        SELECT 1 FROM content_control
        WHERE scope = ? AND target_id = ? AND action = ? AND is_active = 1
        LIMIT 1
        """,
        (scope, target_id, action),
    ) as cur:
        return (await cur.fetchone()) is not None


async def get_control_state(db: aiosqlite.Connection) -> dict[str, list[str]]:
    state = {
        "blocked_identities": [],
        "hidden_identities": [],
        "hidden_threads": [],
        "hidden_posts": [],
        "purged_threads": [],
        "purged_posts": [],
        "banned_attachments": [],
    }
    async with db.execute(
        "SELECT scope, target_id, action FROM content_control WHERE is_active = 1 ORDER BY id ASC"
    ) as cur:
        rows = await cur.fetchall()
    for row in rows:
        scope = row["scope"]
        action = row["action"]
        target_id = row["target_id"]
        if scope == CONTROL_SCOPE_IDENTITY and action == CONTROL_ACTION_BLOCK:
            state["blocked_identities"].append(target_id)
        elif scope == CONTROL_SCOPE_IDENTITY and action == CONTROL_ACTION_HIDE:
            state["hidden_identities"].append(target_id)
        elif scope == CONTROL_SCOPE_THREAD and action == CONTROL_ACTION_HIDE:
            state["hidden_threads"].append(target_id)
        elif scope == CONTROL_SCOPE_POST and action == CONTROL_ACTION_HIDE:
            state["hidden_posts"].append(target_id)
        elif scope == CONTROL_SCOPE_THREAD and action == CONTROL_ACTION_PURGE:
            state["purged_threads"].append(target_id)
        elif scope == CONTROL_SCOPE_POST and action == CONTROL_ACTION_PURGE:
            state["purged_posts"].append(target_id)
        elif scope == CONTROL_SCOPE_ATTACHMENT and action == CONTROL_ACTION_BLOCK:
            state["banned_attachments"].append(target_id)
    return state


async def hide_thread(db: aiosqlite.Connection, thread_id: str, reason: str = "") -> None:
    await set_control(db, scope=CONTROL_SCOPE_THREAD, target_id=thread_id, action=CONTROL_ACTION_HIDE, reason=reason)


async def unhide_thread(db: aiosqlite.Connection, thread_id: str) -> None:
    await clear_control(db, scope=CONTROL_SCOPE_THREAD, target_id=thread_id, action=CONTROL_ACTION_HIDE)


async def hide_post(db: aiosqlite.Connection, post_id: str, reason: str = "") -> None:
    await set_control(db, scope=CONTROL_SCOPE_POST, target_id=post_id, action=CONTROL_ACTION_HIDE, reason=reason)


async def unhide_post(db: aiosqlite.Connection, post_id: str) -> None:
    await clear_control(db, scope=CONTROL_SCOPE_POST, target_id=post_id, action=CONTROL_ACTION_HIDE)


async def mark_post_purged(db: aiosqlite.Connection, post_id: str, reason: str = "") -> None:
    await set_control(db, scope=CONTROL_SCOPE_POST, target_id=post_id, action=CONTROL_ACTION_PURGE, reason=reason)


async def mark_thread_purged(db: aiosqlite.Connection, thread_id: str, reason: str = "") -> None:
    await set_control(db, scope=CONTROL_SCOPE_THREAD, target_id=thread_id, action=CONTROL_ACTION_PURGE, reason=reason)


async def unpurge_post(db: aiosqlite.Connection, post_id: str) -> None:
    """Lift a post purge tombstone.

    Clears the content_control row with action='purge' for this post_id so
    that the post can be re-admitted via gossip.  The metadata and payload
    files were deleted by the original purge and must be re-propagated from
    the network — this call only removes the deny tombstone.
    """
    await clear_control(db, scope=CONTROL_SCOPE_POST, target_id=post_id, action=CONTROL_ACTION_PURGE)


async def unpurge_thread(db: aiosqlite.Connection, thread_id: str) -> None:
    """Lift a thread purge tombstone.

    Clears the content_control row with action='purge' for this thread_id.
    All thread metadata and payloads were deleted by the original purge and
    must be re-propagated from the network.
    """
    await clear_control(db, scope=CONTROL_SCOPE_THREAD, target_id=thread_id, action=CONTROL_ACTION_PURGE)


# ── Identity control ────────────────────────────────────────────────────────


async def hide_identity(db: aiosqlite.Connection, identity_hash: str, reason: str = "") -> None:
    await set_control(db, scope=CONTROL_SCOPE_IDENTITY, target_id=identity_hash, action=CONTROL_ACTION_HIDE, reason=reason)


async def unhide_identity(db: aiosqlite.Connection, identity_hash: str) -> None:
    await clear_control(db, scope=CONTROL_SCOPE_IDENTITY, target_id=identity_hash, action=CONTROL_ACTION_HIDE)


async def block_identity(db: aiosqlite.Connection, identity_hash: str, reason: str = "") -> None:
    await set_control(db, scope=CONTROL_SCOPE_IDENTITY, target_id=identity_hash, action=CONTROL_ACTION_BLOCK, reason=reason)


async def unblock_identity(db: aiosqlite.Connection, identity_hash: str) -> None:
    await clear_control(db, scope=CONTROL_SCOPE_IDENTITY, target_id=identity_hash, action=CONTROL_ACTION_BLOCK)


async def is_identity_blocked(db: aiosqlite.Connection, identity_hash: str) -> bool:
    return bool(identity_hash) and await has_control(
        db, scope=CONTROL_SCOPE_IDENTITY, target_id=identity_hash, action=CONTROL_ACTION_BLOCK,
    )


async def is_identity_hidden(db: aiosqlite.Connection, identity_hash: str) -> bool:
    return bool(identity_hash) and await has_control(
        db, scope=CONTROL_SCOPE_IDENTITY, target_id=identity_hash, action=CONTROL_ACTION_HIDE,
    )


# ── Attachment control ──────────────────────────────────────────────────────


async def ban_attachment(db: aiosqlite.Connection, attachment_content_hash: str, reason: str = "") -> None:
    await set_control(
        db, scope=CONTROL_SCOPE_ATTACHMENT, target_id=attachment_content_hash,
        action=CONTROL_ACTION_BLOCK, reason=reason,
    )


async def unban_attachment(db: aiosqlite.Connection, attachment_content_hash: str) -> None:
    await clear_control(
        db, scope=CONTROL_SCOPE_ATTACHMENT, target_id=attachment_content_hash,
        action=CONTROL_ACTION_BLOCK,
    )


async def is_attachment_banned(db: aiosqlite.Connection, attachment_content_hash: str) -> bool:
    return bool(attachment_content_hash) and await has_control(
        db, scope=CONTROL_SCOPE_ATTACHMENT, target_id=attachment_content_hash,
        action=CONTROL_ACTION_BLOCK,
    )


# ── Identity lookup for ban cascade ─────────────────────────────────────────


async def get_posts_by_identity(
    db: aiosqlite.Connection,
    identity_hash: str,
) -> list[PostMetadata]:
    """Return all posts by a given identity hash. Used for ban cascade."""
    async with db.execute(
        "SELECT * FROM posts WHERE identity_hash = ?",
        (identity_hash,),
    ) as cur:
        rows = await cur.fetchall()
    return [
        PostMetadata(
            post_id=row["post_id"],
            thread_id=row["thread_id"],
            parent_id=row["parent_id"],
            timestamp=row["timestamp"],
            expiry_timestamp=row["expiry_timestamp"],
            bump_flag=bool(row["bump_flag"]),
            content_hash=row["content_hash"],
            payload_size=row["payload_size"],
            attachment_content_hash=row["attachment_content_hash"],
            attachment_payload_size=row["attachment_payload_size"],
            has_attachments=bool(row["has_attachments"]),
            attachment_count=row["attachment_count"] if "attachment_count" in row.keys() else 0,
            text_only=bool(row["text_only"]),
            identity_hash=row["identity_hash"],
            pow_nonce=row["pow_nonce"],
            public_key=row.get("public_key", "") if hasattr(row, "get") else (row["public_key"] if "public_key" in row.keys() else ""),
            encrypted_pings=(
                json.loads(row["encrypted_pings"])
                if "encrypted_pings" in row.keys() and row["encrypted_pings"]
                else []
            ),
            edit_signature=row["edit_signature"] if "edit_signature" in row.keys() else "",
            thread_last_activity=row["thread_last_activity"],
            is_abandoned=bool(row["is_abandoned"]),
        )
        for row in rows
    ]


# ── Moderation action log ───────────────────────────────────────────────────


async def create_moderation_action(
    db: aiosqlite.Connection,
    action_kind: str,
    target_id: str,
    reason: str = "",
) -> int:
    """Create a moderation action record. Returns the action id."""
    cursor = await db.execute(
        """
        INSERT INTO moderation_actions (action_kind, target_id, created_at, reason, is_active)
        VALUES (?, ?, ?, ?, 1)
        """,
        (action_kind, target_id, int(time.time()), reason),
    )
    await db.commit()
    return cursor.lastrowid


async def record_moderation_target(
    db: aiosqlite.Connection,
    action_id: int,
    target_scope: str,
    target_id: str,
) -> None:
    """Record a post/thread purged as part of a moderation action."""
    await db.execute(
        """
        INSERT OR IGNORE INTO moderation_action_targets (action_id, target_scope, target_id)
        VALUES (?, ?, ?)
        """,
        (action_id, target_scope, target_id),
    )
    await db.commit()


async def reverse_moderation_action(db: aiosqlite.Connection, action_id: int) -> int:
    """Reverse a moderation action by lifting the purge tombstones it created.

    Returns the number of tombstones lifted.
    """
    async with db.execute(
        "SELECT target_scope, target_id FROM moderation_action_targets WHERE action_id = ?",
        (action_id,),
    ) as cur:
        targets = await cur.fetchall()

    lifted = 0
    for row in targets:
        scope = CONTROL_SCOPE_THREAD if row["target_scope"] == "thread" else CONTROL_SCOPE_POST
        await db.execute(
            """
            UPDATE content_control SET is_active = 0
            WHERE scope = ? AND target_id = ? AND action = 'purge' AND is_active = 1
            """,
            (scope, row["target_id"]),
        )
        lifted += 1

    await db.execute(
        """
        UPDATE moderation_actions SET is_active = 0, reversed_at = ?
        WHERE id = ?
        """,
        (int(time.time()), action_id),
    )
    await db.commit()
    return lifted


async def get_active_moderation_action(
    db: aiosqlite.Connection,
    action_kind: str,
    target_id: str,
) -> Optional[int]:
    """Return the active moderation action id for a given kind and target, or None."""
    async with db.execute(
        """
        SELECT id FROM moderation_actions
        WHERE action_kind = ? AND target_id = ? AND is_active = 1
        ORDER BY created_at DESC LIMIT 1
        """,
        (action_kind, target_id),
    ) as cur:
        row = await cur.fetchone()
    return row["id"] if row else None


async def get_banned_list(db: aiosqlite.Connection) -> dict:
    """Return banned identities and attachments with metadata for the Ban UI."""
    identities = []
    async with db.execute(
        """
        SELECT target_id, created_at, reason
        FROM content_control
        WHERE scope = 'identity' AND action = 'block' AND is_active = 1
        ORDER BY created_at DESC
        """,
    ) as cur:
        for row in await cur.fetchall():
            identities.append({
                "target_id": row["target_id"],
                "created_at": row["created_at"],
                "reason": row["reason"],
            })

    attachments = []
    async with db.execute(
        """
        SELECT target_id, created_at, reason
        FROM content_control
        WHERE scope = 'attachment' AND action = 'block' AND is_active = 1
        ORDER BY created_at DESC
        """,
    ) as cur:
        for row in await cur.fetchall():
            attachments.append({
                "target_id": row["target_id"],
                "created_at": row["created_at"],
                "reason": row["reason"],
            })

    return {"identities": identities, "attachments": attachments}


async def get_post_blob_references(db: aiosqlite.Connection, post_id: str) -> list[str]:
    async with db.execute(
        "SELECT content_hash, attachment_content_hash FROM posts WHERE post_id = ? LIMIT 1",
        (post_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return []
    hashes = [row["content_hash"]]
    if row["attachment_content_hash"]:
        hashes.append(row["attachment_content_hash"])
    return [h for h in hashes if h]


async def get_thread_blob_references(db: aiosqlite.Connection, thread_id: str) -> list[str]:
    async with db.execute(
        "SELECT content_hash, attachment_content_hash FROM posts WHERE thread_id = ?",
        (thread_id,),
    ) as cur:
        rows = await cur.fetchall()
    hashes: list[str] = []
    for row in rows:
        if row["content_hash"]:
            hashes.append(row["content_hash"])
        if row["attachment_content_hash"]:
            hashes.append(row["attachment_content_hash"])
    return hashes


async def delete_post_metadata(db: aiosqlite.Connection, post_id: str) -> int:
    cur = await db.execute("DELETE FROM posts WHERE post_id = ?", (post_id,))
    await db.commit()
    return int(cur.rowcount or 0)


async def delete_thread_metadata(db: aiosqlite.Connection, thread_id: str) -> int:
    cur = await db.execute("DELETE FROM posts WHERE thread_id = ?", (thread_id,))
    await db.commit()
    return int(cur.rowcount or 0)


# =============================================================================
# Chunk manifest / session CRUD (Phase 1 foundation)
# =============================================================================

async def save_chunk_manifest(
    db: aiosqlite.Connection,
    manifest: ChunkManifest,
    entries: list[ChunkManifestEntry],
    *,
    expires_at: int = 0,
) -> None:
    """Persist one structural chunk manifest and all of its entries."""
    await db.execute(
        """
        INSERT OR REPLACE INTO chunk_manifests (
            blob_hash, board_id, post_id, thread_id, blob_kind,
            blob_size, chunk_size, chunk_count, merkle_root,
            manifest_version, created_at, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            manifest.blob_hash,
            manifest.board_id,
            manifest.post_id,
            manifest.thread_id,
            manifest.blob_kind,
            manifest.blob_size,
            manifest.chunk_size,
            manifest.chunk_count,
            manifest.merkle_root,
            manifest.manifest_version,
            manifest.created_at,
            expires_at,
        ),
    )
    await db.execute(
        "DELETE FROM chunk_manifest_entries WHERE blob_hash = ?",
        (manifest.blob_hash,),
    )
    await db.executemany(
        """
        INSERT INTO chunk_manifest_entries (
            blob_hash, chunk_index, offset, size, chunk_hash
        ) VALUES (?, ?, ?, ?, ?)
        """,
        [
            (
                entry.blob_hash,
                entry.chunk_index,
                entry.offset,
                entry.size,
                entry.chunk_hash,
            )
            for entry in entries
        ],
    )
    await db.commit()


async def load_chunk_manifest(
    db: aiosqlite.Connection,
    blob_hash: str,
) -> tuple[ChunkManifest, list[ChunkManifestEntry]] | None:
    """Load a manifest and all of its entries for a blob hash."""
    async with db.execute(
        "SELECT * FROM chunk_manifests WHERE blob_hash = ? LIMIT 1",
        (blob_hash,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None

    manifest = ChunkManifest(
        manifest_version=row["manifest_version"],
        board_id=row["board_id"],
        post_id=row["post_id"],
        thread_id=row["thread_id"],
        blob_kind=row["blob_kind"],
        blob_hash=row["blob_hash"],
        blob_size=row["blob_size"],
        chunk_size=row["chunk_size"],
        chunk_count=row["chunk_count"],
        merkle_root=row["merkle_root"],
        created_at=row["created_at"],
    )

    async with db.execute(
        "SELECT * FROM chunk_manifest_entries WHERE blob_hash = ? ORDER BY chunk_index ASC",
        (blob_hash,),
    ) as cur:
        entries_rows = await cur.fetchall()
    entries = [
        ChunkManifestEntry(
            blob_hash=entry["blob_hash"],
            chunk_index=entry["chunk_index"],
            offset=entry["offset"],
            size=entry["size"],
            chunk_hash=entry["chunk_hash"],
        )
        for entry in entries_rows
    ]
    return manifest, entries


async def delete_chunk_manifests_for_blobs(
    db: aiosqlite.Connection,
    blob_hashes: list[str],
) -> int:
    """Backward-compatible wrapper for manifest deletion only count."""
    result = await delete_chunk_transfer_state_for_blobs(db, blob_hashes)
    return int(result["deleted_manifests"])


async def delete_chunk_transfer_state_for_blobs(
    db: aiosqlite.Connection,
    blob_hashes: list[str],
) -> dict[str, int]:
    """Delete persisted chunk transfer state for the given blob hashes.

    This removes structural-only transport state tied directly to the
    canonical blob identity: fetch sessions, per-chunk request states,
    manifests, and peer availability summaries. Peer penalties are not
    touched because the current schema tracks them per peer/board rather
    than per blob.
    """
    if not blob_hashes:
        return {
            "deleted_manifests": 0,
            "deleted_sessions": 0,
            "deleted_request_states": 0,
            "deleted_availability_rows": 0,
        }

    placeholders = ",".join("?" for _ in blob_hashes)

    async with db.execute(
        f"SELECT COUNT(*) FROM chunk_manifests WHERE blob_hash IN ({placeholders})",
        blob_hashes,
    ) as cur:
        row = await cur.fetchone()
        manifest_count = int(row[0]) if row is not None else 0

    async with db.execute(
        f"SELECT session_id FROM chunk_fetch_sessions WHERE blob_hash IN ({placeholders})",
        blob_hashes,
    ) as cur:
        session_rows = await cur.fetchall()
    session_ids = [str(row[0]) for row in session_rows]
    session_count = len(session_ids)

    request_state_count = 0
    if session_ids:
        session_placeholders = ",".join("?" for _ in session_ids)
        async with db.execute(
            f"SELECT COUNT(*) FROM chunk_request_states WHERE session_id IN ({session_placeholders})",
            session_ids,
        ) as cur:
            row = await cur.fetchone()
            request_state_count = int(row[0]) if row is not None else 0
        await db.execute(
            f"DELETE FROM chunk_request_states WHERE session_id IN ({session_placeholders})",
            session_ids,
        )

    async with db.execute(
        f"SELECT COUNT(*) FROM peer_chunk_availability WHERE blob_hash IN ({placeholders})",
        blob_hashes,
    ) as cur:
        row = await cur.fetchone()
        availability_count = int(row[0]) if row is not None else 0

    await db.execute(
        f"DELETE FROM chunk_manifest_entries WHERE blob_hash IN ({placeholders})",
        blob_hashes,
    )
    await db.execute(
        f"DELETE FROM chunk_manifests WHERE blob_hash IN ({placeholders})",
        blob_hashes,
    )
    await db.execute(
        f"DELETE FROM chunk_fetch_sessions WHERE blob_hash IN ({placeholders})",
        blob_hashes,
    )
    await db.execute(
        f"DELETE FROM peer_chunk_availability WHERE blob_hash IN ({placeholders})",
        blob_hashes,
    )
    await db.commit()

    return {
        "deleted_manifests": manifest_count,
        "deleted_sessions": session_count,
        "deleted_request_states": request_state_count,
        "deleted_availability_rows": availability_count,
    }


async def save_chunk_fetch_session(
    db: aiosqlite.Connection,
    session: ChunkFetchSession,
) -> None:
    """Insert or update a chunk fetch session."""
    await db.execute(
        """
        INSERT OR REPLACE INTO chunk_fetch_sessions (
            session_id, board_id, blob_hash, blob_kind, state,
            request_peer_lxmf_hash, started_at, updated_at, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session.session_id,
            session.board_id,
            session.blob_hash,
            session.blob_kind,
            session.state,
            session.request_peer_lxmf_hash,
            session.started_at,
            session.updated_at,
            session.expires_at,
        ),
    )
    await db.commit()


async def load_chunk_fetch_session(
    db: aiosqlite.Connection,
    session_id: str,
) -> ChunkFetchSession | None:
    """Load one persisted chunk fetch session by id."""
    async with db.execute(
        "SELECT * FROM chunk_fetch_sessions WHERE session_id = ? LIMIT 1",
        (session_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return ChunkFetchSession(
        session_id=row["session_id"],
        board_id=row["board_id"],
        blob_hash=row["blob_hash"],
        blob_kind=row["blob_kind"],
        state=row["state"],
        request_peer_lxmf_hash=row["request_peer_lxmf_hash"],
        started_at=row["started_at"],
        updated_at=row["updated_at"],
        expires_at=row["expires_at"],
    )


async def load_latest_chunk_fetch_session_for_blob(
    db: aiosqlite.Connection,
    *,
    board_id: str,
    blob_hash: str,
) -> ChunkFetchSession | None:
    """Load the most recent persisted chunk fetch session for a blob."""
    async with db.execute(
        """
        SELECT * FROM chunk_fetch_sessions
        WHERE board_id = ? AND blob_hash = ?
        ORDER BY updated_at DESC, started_at DESC
        LIMIT 1
        """,
        (board_id, blob_hash),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return ChunkFetchSession(
        session_id=row["session_id"],
        board_id=row["board_id"],
        blob_hash=row["blob_hash"],
        blob_kind=row["blob_kind"],
        state=row["state"],
        request_peer_lxmf_hash=row["request_peer_lxmf_hash"],
        started_at=row["started_at"],
        updated_at=row["updated_at"],
        expires_at=row["expires_at"],
    )


async def save_chunk_request_state(
    db: aiosqlite.Connection,
    state: ChunkRequestStateRecord,
) -> None:
    await db.execute(
        """
        INSERT OR REPLACE INTO chunk_request_states (
            session_id, chunk_index, state, assigned_peer_lxmf_hash,
            request_id, attempt_count, deadline_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            state.session_id,
            state.chunk_index,
            state.state,
            state.assigned_peer_lxmf_hash,
            state.request_id,
            int(state.attempt_count),
            int(state.deadline_at),
            int(state.updated_at),
        ),
    )
    await db.commit()


async def load_chunk_request_states(
    db: aiosqlite.Connection,
    session_id: str,
) -> list[ChunkRequestStateRecord]:
    async with db.execute(
        "SELECT * FROM chunk_request_states WHERE session_id = ? ORDER BY chunk_index ASC",
        (session_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [
        ChunkRequestStateRecord(
            session_id=row["session_id"],
            chunk_index=int(row["chunk_index"]),
            state=row["state"],
            assigned_peer_lxmf_hash=row["assigned_peer_lxmf_hash"],
            request_id=row["request_id"],
            attempt_count=int(row["attempt_count"]),
            deadline_at=int(row["deadline_at"]),
            updated_at=int(row["updated_at"]),
        )
        for row in rows
    ]


async def delete_chunk_request_state(
    db: aiosqlite.Connection,
    *,
    session_id: str,
    chunk_index: int,
) -> None:
    await db.execute(
        "DELETE FROM chunk_request_states WHERE session_id = ? AND chunk_index = ?",
        (session_id, int(chunk_index)),
    )
    await db.commit()


async def upsert_chunk_peer_penalty(
    db: aiosqlite.Connection,
    record: ChunkPeerPenaltyRecord,
) -> None:
    await db.execute(
        """
        INSERT OR REPLACE INTO chunk_peer_penalties (
            board_id, peer_lxmf_hash, timeout_count, invalid_chunk_count,
            success_count, cooldown_until, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.board_id,
            record.peer_lxmf_hash,
            int(record.timeout_count),
            int(record.invalid_chunk_count),
            int(record.success_count),
            int(record.cooldown_until),
            int(record.updated_at),
        ),
    )
    await db.commit()


async def load_chunk_peer_penalties(
    db: aiosqlite.Connection,
    *,
    board_id: str,
    peer_lxmf_hashes: list[str] | None = None,
) -> dict[str, ChunkPeerPenaltyRecord]:
    query = "SELECT * FROM chunk_peer_penalties WHERE board_id = ?"
    params: list[object] = [board_id]
    if peer_lxmf_hashes:
        placeholders = ",".join("?" for _ in peer_lxmf_hashes)
        query += f" AND peer_lxmf_hash IN ({placeholders})"
        params.extend(peer_lxmf_hashes)
    async with db.execute(query, tuple(params)) as cur:
        rows = await cur.fetchall()
    return {
        row["peer_lxmf_hash"]: ChunkPeerPenaltyRecord(
            board_id=row["board_id"],
            peer_lxmf_hash=row["peer_lxmf_hash"],
            timeout_count=int(row["timeout_count"]),
            invalid_chunk_count=int(row["invalid_chunk_count"]),
            success_count=int(row["success_count"]),
            cooldown_until=int(row["cooldown_until"]),
            updated_at=int(row["updated_at"]),
        )
        for row in rows
    }


async def upsert_peer_chunk_availability(
    db: aiosqlite.Connection,
    *,
    board_id: str,
    peer_lxmf_hash: str,
    blob_hash: str,
    chunk_count: int,
    complete: bool,
    ranges: list[tuple[int, int]],
    last_seen_at: int | None = None,
) -> None:
    """Persist structural per-peer availability for one blob."""
    seen_at = int(time.time()) if last_seen_at is None else int(last_seen_at)
    await db.execute(
        """
        INSERT OR REPLACE INTO peer_chunk_availability (
            board_id, peer_lxmf_hash, blob_hash, chunk_count,
            complete, ranges_json, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            board_id,
            peer_lxmf_hash,
            blob_hash,
            int(chunk_count),
            1 if complete else 0,
            json.dumps([[int(start), int(end)] for start, end in ranges], separators=(",", ":")),
            seen_at,
        ),
    )
    await db.commit()


async def load_peer_chunk_availability(
    db: aiosqlite.Connection,
    *,
    board_id: str,
    blob_hash: str,
) -> dict[str, dict]:
    """Load structural availability records for all peers of one blob."""
    async with db.execute(
        """
        SELECT peer_lxmf_hash, chunk_count, complete, ranges_json, last_seen_at
        FROM peer_chunk_availability
        WHERE board_id = ? AND blob_hash = ?
        ORDER BY last_seen_at DESC
        """,
        (board_id, blob_hash),
    ) as cur:
        rows = await cur.fetchall()
    result: dict[str, dict] = {}
    for row in rows:
        try:
            raw_ranges = json.loads(row["ranges_json"] or "[]")
        except Exception:
            raw_ranges = []
        ranges: list[tuple[int, int]] = []
        for item in raw_ranges:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            ranges.append((int(item[0]), int(item[1])))
        result[str(row["peer_lxmf_hash"])] = {
            "chunk_count": int(row["chunk_count"]),
            "complete": bool(row["complete"]),
            "ranges": ranges,
            "last_seen_at": int(row["last_seen_at"]),
        }
    return result


async def delete_peer_chunk_availability_for_blobs(
    db: aiosqlite.Connection,
    blob_hashes: list[str],
) -> int:
    if not blob_hashes:
        return 0
    placeholders = ",".join("?" for _ in blob_hashes)
    async with db.execute(
        f"SELECT COUNT(*) FROM peer_chunk_availability WHERE blob_hash IN ({placeholders})",
        blob_hashes,
    ) as cur:
        row = await cur.fetchone()
        count = int(row[0]) if row is not None else 0
    await db.execute(
        f"DELETE FROM peer_chunk_availability WHERE blob_hash IN ({placeholders})",
        blob_hashes,
    )
    await db.commit()
    return count


# =============================================================================
# Helpers
# =============================================================================


def _parse_encrypted_pings(raw_value: object) -> list[str]:
    """Parse stored encrypted_pings JSON into a list of strings."""
    if isinstance(raw_value, list):
        return [item for item in raw_value if isinstance(item, str)]

    if not raw_value:
        return []

    try:
        parsed = json.loads(raw_value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []

    if not isinstance(parsed, list):
        return []

    return [item for item in parsed if isinstance(item, str)]


def _row_to_post(row: aiosqlite.Row) -> PostMetadata:
    """Convert a database row to a PostMetadata dataclass."""
    return PostMetadata(
        post_id=row["post_id"],
        thread_id=row["thread_id"],
        parent_id=row["parent_id"],
        timestamp=row["timestamp"],
        expiry_timestamp=row["expiry_timestamp"],
        bump_flag=bool(row["bump_flag"]),
        content_hash=row["content_hash"],
        payload_size=row["payload_size"],
        attachment_content_hash=row["attachment_content_hash"] if "attachment_content_hash" in row.keys() else "",
        attachment_payload_size=row["attachment_payload_size"] if "attachment_payload_size" in row.keys() else 0,
        has_attachments=bool(row["has_attachments"]),
        attachment_count=row["attachment_count"] if "attachment_count" in row.keys() else 0,
        text_only=bool(row["text_only"]),
        identity_hash=row["identity_hash"],
        pow_nonce=row["pow_nonce"],
        public_key=row["public_key"] if "public_key" in row.keys() else "",
        encrypted_pings=_parse_encrypted_pings(
            row["encrypted_pings"] if "encrypted_pings" in row.keys() else "[]"
        ),
        edit_signature=row["edit_signature"] if "edit_signature" in row.keys() else "",
        thread_last_activity=row["thread_last_activity"],
        is_abandoned=bool(row["is_abandoned"]),
    )
