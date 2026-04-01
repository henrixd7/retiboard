"""
Purge operations for local content control.

Spec references:
    §1  — Aggressive ephemeral defaults; abandoned threads fully deleted.
    §4  — Pruning rules and disk layout (payloads, chunk_cache).
    §19 — Moderation is local-only.
    §22 — No infrastructure component may enforce retention policy.

Design:
    Purge = immediate hard local deletion + persistent deny tombstone.

    The deny tombstone (written via mark_post_purged / mark_thread_purged)
    lives in content_control with is_active=1.  It survives deletion of
    the post metadata rows, so if a peer later re-gossips the same
    post_id/thread_id it will be rejected again at admission (receiver.py).

    Ordering matters:
        1. Collect blob hashes while the post rows still exist.
        2. Delete post metadata rows.
        3. Delete payload files and chunk cache directories.
        4. Delete chunk transfer state (manifests, sessions, request states,
           peer availability rows) — these FK-reference blob_hash, so they
           must go after the blob files are gone to avoid orphaned state.
        5. Write the tombstone — last, so a crash between steps 1–4 leaves
           no tombstone and the next purge attempt can retry cleanly.

"""

from __future__ import annotations

from dataclasses import dataclass, field

from retiboard.db.database import (
    create_moderation_action,
    delete_chunk_transfer_state_for_blobs,
    delete_post_metadata,
    delete_thread_metadata,
    get_post,
    get_post_blob_references,
    get_posts_by_identity,
    get_thread_blob_references,
    mark_post_purged,
    mark_thread_purged,
    record_moderation_target,
    recompute_thread_lifecycle,
)
from retiboard.storage.payloads import delete_chunk_cache_bulk, delete_payloads_bulk
from retiboard.sync.payload_fetch import cancel_pending_chunk_session


@dataclass(frozen=True)
class PurgeResult:
    deleted_posts: int
    deleted_payload_blobs: int
    deleted_chunk_caches: int
    deleted_manifests: int
    deleted_sessions: int
    deleted_request_states: int
    deleted_availability_rows: int
    tombstoned_scope: str
    tombstoned_target_id: str
    purged_post_ids: list[str] = field(default_factory=list)

    def __add__(self, other: "PurgeResult") -> "PurgeResult":
        """Combine two PurgeResult instances."""
        return PurgeResult(
            deleted_posts=self.deleted_posts + other.deleted_posts,
            deleted_payload_blobs=self.deleted_payload_blobs + other.deleted_payload_blobs,
            deleted_chunk_caches=self.deleted_chunk_caches + other.deleted_chunk_caches,
            deleted_manifests=self.deleted_manifests + other.deleted_manifests,
            deleted_sessions=self.deleted_sessions + other.deleted_sessions,
            deleted_request_states=self.deleted_request_states + other.deleted_request_states,
            deleted_availability_rows=self.deleted_availability_rows + other.deleted_availability_rows,
            tombstoned_scope=self.tombstoned_scope,
            tombstoned_target_id=self.tombstoned_target_id,
            purged_post_ids=self.purged_post_ids + other.purged_post_ids,
        )


def _empty_result(scope: str, target_id: str) -> PurgeResult:
    return PurgeResult(0, 0, 0, 0, 0, 0, 0, scope, target_id, [])


async def purge_post(db, board_id: str, post_id: str, reason: str = "") -> PurgeResult:
    """
    Purge a single post and its payloads.

    If post_id is a thread OP (post_id == thread_id), we redirect to
    purge_thread() automatically.  Deleting only the OP row would leave
    all reply rows as orphans: they can never be pruned by the normal
    pruner path (which relies on the OP row's is_abandoned flag), they
    would still appear in delta gossip, and they have no parent to render
    against in the UI.

    Args:
        db: Open aiosqlite connection for this board's meta.db.
        board_id: Board identifier (used for payload/chunk-cache paths).
        post_id: Post to purge.
        reason: Optional human-readable label stored in the tombstone.

    Returns:
        PurgeResult with deletion counts and tombstone info.
    """
    # Check whether this post is a thread OP.
    post_row = await get_post(db, post_id)
    if post_row is not None and post_row.post_id == post_row.thread_id:
        # Redirect: purging an OP must purge the whole thread.
        return await purge_thread(db, board_id, post_row.thread_id, reason=reason)

    # Normal single-post purge.
    blob_hashes = await get_post_blob_references(db, post_id)
    deleted_posts = await delete_post_metadata(db, post_id)
    deleted_payload_blobs = delete_payloads_bulk(board_id, blob_hashes)
    deleted_chunk_caches = delete_chunk_cache_bulk(board_id, blob_hashes)
    chunk_state = await delete_chunk_transfer_state_for_blobs(db, blob_hashes)
    for blob_hash in blob_hashes:
        await cancel_pending_chunk_session(blob_hash)
    if post_row is not None:
        await recompute_thread_lifecycle(db, post_row.thread_id)
        await db.commit()
    await mark_post_purged(db, post_id, reason=reason)

    return PurgeResult(
        deleted_posts=deleted_posts,
        deleted_payload_blobs=deleted_payload_blobs,
        deleted_chunk_caches=deleted_chunk_caches,
        deleted_manifests=int(chunk_state["deleted_manifests"]),
        deleted_sessions=int(chunk_state["deleted_sessions"]),
        deleted_request_states=int(chunk_state["deleted_request_states"]),
        deleted_availability_rows=int(chunk_state["deleted_availability_rows"]),
        tombstoned_scope="post",
        tombstoned_target_id=post_id,
        purged_post_ids=[post_id] if deleted_posts > 0 else [],
    )


async def purge_thread(db, board_id: str, thread_id: str, reason: str = "") -> PurgeResult:
    """
    Purge an entire thread: all posts, payloads, chunk state, and tombstone.

    Args:
        db: Open aiosqlite connection for this board's meta.db.
        board_id: Board identifier.
        thread_id: Thread OP id (== thread identifier).
        reason: Optional human-readable label stored in the tombstone.

    Returns:
        PurgeResult with deletion counts and tombstone info.
    """
    # Collect all post IDs in the thread before deletion for frontend eviction.
    async with db.execute("SELECT post_id FROM posts WHERE thread_id = ?", (thread_id,)) as cur:
        purged_ids = [row[0] for row in await cur.fetchall()]

    blob_hashes = await get_thread_blob_references(db, thread_id)
    deleted_posts = await delete_thread_metadata(db, thread_id)
    deleted_payload_blobs = delete_payloads_bulk(board_id, blob_hashes)
    deleted_chunk_caches = delete_chunk_cache_bulk(board_id, blob_hashes)
    chunk_state = await delete_chunk_transfer_state_for_blobs(db, blob_hashes)
    for blob_hash in blob_hashes:
        await cancel_pending_chunk_session(blob_hash)
    await mark_thread_purged(db, thread_id, reason=reason)

    return PurgeResult(
        deleted_posts=deleted_posts,
        deleted_payload_blobs=deleted_payload_blobs,
        deleted_chunk_caches=deleted_chunk_caches,
        deleted_manifests=int(chunk_state["deleted_manifests"]),
        deleted_sessions=int(chunk_state["deleted_sessions"]),
        deleted_request_states=int(chunk_state["deleted_request_states"]),
        deleted_availability_rows=int(chunk_state["deleted_availability_rows"]),
        tombstoned_scope="thread",
        tombstoned_target_id=thread_id,
        purged_post_ids=purged_ids,
    )


async def purge_identity(
    db,
    board_id: str,
    identity_hash: str,
    reason: str = "",
) -> tuple[int, PurgeResult]:
    """Purge all posts by an identity as part of an identity ban.

    Deduplication: if a thread's OP belongs to this identity, purge the
    whole thread rather than individual replies (they would become orphans).
    Replies in threads owned by other identities are purged individually.

    Records every purged target in moderation_action_targets so the
    cascade can be selectively reversed on unban.

    Returns (action_id, combined PurgeResult).
    """
    posts = await get_posts_by_identity(db, identity_hash)
    action_id = await create_moderation_action(db, "identity_ban", identity_hash, reason)

    combined = _empty_result("identity", identity_hash)

    # Collect thread IDs where this identity is the OP.
    op_thread_ids: set[str] = set()
    for post in posts:
        if post.post_id == post.thread_id:
            op_thread_ids.add(post.thread_id)

    purged_threads: set[str] = set()
    purged_posts: set[str] = set()

    # First pass: purge threads where this identity is OP.
    for thread_id in op_thread_ids:
        result = await purge_thread(db, board_id, thread_id, reason=reason)
        combined = combined + result
        purged_threads.add(thread_id)
        await record_moderation_target(db, action_id, "thread", thread_id)

    # Second pass: purge individual replies in threads owned by others.
    for post in posts:
        if post.thread_id in purged_threads:
            continue  # Already handled by thread purge.
        if post.post_id in purged_posts:
            continue
        result = await purge_post(db, board_id, post.post_id, reason=reason)
        combined = combined + result
        purged_posts.add(post.post_id)
        await record_moderation_target(db, action_id, "post", post.post_id)

    return action_id, combined

