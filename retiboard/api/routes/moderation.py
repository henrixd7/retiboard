"""Local structural moderation API routes.

Spec references:
    §2.2 / §2.4 — relay mode and full clients must enforce identical
                   storage/retention behavior
    §3.1 / §5    — structural-only backend, no plaintext inspection
    §7.4 / §22   — bandwidth and retention remain bounded by local policy
    §19          — moderation is local-only

Design invariants:
    - All moderation state is board-local in that board's meta.db.
    - The backend stores only structural controls: identity hash, thread id,
      post id. No plaintext content filters live here.
    - Purge is a hard local deletion plus a persistent deny tombstone.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from retiboard.db.database import (
    ban_attachment,
    block_identity,
    get_active_moderation_action,
    get_banned_list,
    get_control_state,
    hide_identity,
    hide_post,
    hide_thread,
    open_existing_board_db,
    reverse_moderation_action,
    unban_attachment,
    unblock_identity,
    unhide_identity,
    unhide_post,
    unhide_thread,
    unpurge_post,
    unpurge_thread,
)
from retiboard.db.database import delete_chunk_transfer_state_for_blobs
from retiboard.moderation.purge import purge_identity, purge_post, purge_thread
from retiboard.storage.payloads import delete_chunk_cache_bulk, delete_payloads_bulk
from retiboard.sync.payload_fetch import cancel_pending_chunk_session


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class ThreadControlRequest(BaseModel):
    thread_id: str = Field(..., min_length=1, max_length=128)
    reason: str = Field(default="", max_length=512)


class PostControlRequest(BaseModel):
    post_id: str = Field(..., min_length=1, max_length=128)
    reason: str = Field(default="", max_length=512)


class IdentityControlRequest(BaseModel):
    identity_hash: str = Field(..., min_length=1, max_length=128)
    reason: str = Field(default="", max_length=512)


class AttachmentControlRequest(BaseModel):
    attachment_content_hash: str = Field(..., min_length=1, max_length=128)
    reason: str = Field(default="", max_length=512)


class ControlStateResponse(BaseModel):
    blocked_identities: list[str]
    hidden_identities: list[str]
    hidden_threads: list[str]
    hidden_posts: list[str]
    purged_threads: list[str]
    purged_posts: list[str]
    banned_attachments: list[str]


class ControlMutationResponse(BaseModel):
    ok: bool
    action: str
    scope: str
    target_id: str


class PurgeResponse(BaseModel):
    ok: bool
    action: str
    scope: str
    target_id: str
    deleted_posts: int
    deleted_payload_blobs: int
    deleted_chunk_caches: int
    deleted_manifests: int
    deleted_sessions: int
    deleted_request_states: int
    deleted_availability_rows: int
    purged_post_ids: list[str] = []


class IdentityBanResponse(BaseModel):
    ok: bool
    action: str
    identity_hash: str
    action_id: int
    deleted_posts: int
    deleted_payload_blobs: int
    deleted_chunk_caches: int
    deleted_manifests: int
    purged_post_ids: list[str] = []


class BannedItemResponse(BaseModel):
    target_id: str
    created_at: int
    reason: str


class BannedListResponse(BaseModel):
    identities: list[BannedItemResponse]
    attachments: list[BannedItemResponse]


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------

def create_moderation_router(sync_engine=None) -> APIRouter:
    router = APIRouter(prefix="/api/boards/{board_id}/control", tags=["moderation"])

    async def _open_subscribed_board_or_404(board_id: str):
        try:
            return await open_existing_board_db(board_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Board not found")

    # ── State query ──────────────────────────────────────────────────────────

    @router.get("/state", response_model=ControlStateResponse)
    async def get_board_control_state(board_id: str):
        """Return all active content-control rules for a board.

        The frontend Pinia store calls this on board open to hydrate its
        reactive sets (hidden_threads, hidden_posts, purged_*).
        """
        db = await _open_subscribed_board_or_404(board_id)
        try:
            return ControlStateResponse(**(await get_control_state(db)))
        finally:
            await db.close()

    # ── Thread hide / unhide ─────────────────────────────────────────────────

    @router.post("/hide-thread", response_model=ControlMutationResponse)
    async def hide_thread_route(board_id: str, req: ThreadControlRequest):
        """Hide a thread locally. It is not gossipped to peers while hidden."""
        db = await _open_subscribed_board_or_404(board_id)
        try:
            await hide_thread(db, req.thread_id, reason=req.reason)
            return ControlMutationResponse(
                ok=True, action="hide", scope="thread", target_id=req.thread_id,
            )
        finally:
            await db.close()

    @router.delete("/hide-thread/{thread_id}", response_model=ControlMutationResponse)
    async def unhide_thread_route(board_id: str, thread_id: str):
        """Lift a thread hide. The thread will be gossipped again."""
        db = await _open_subscribed_board_or_404(board_id)
        try:
            await unhide_thread(db, thread_id)
            return ControlMutationResponse(
                ok=True, action="unhide", scope="thread", target_id=thread_id,
            )
        finally:
            await db.close()

    # ── Post hide / unhide ───────────────────────────────────────────────────

    @router.post("/hide-post", response_model=ControlMutationResponse)
    async def hide_post_route(board_id: str, req: PostControlRequest):
        """Hide a single post locally. Its payload is not served to peers."""
        db = await _open_subscribed_board_or_404(board_id)
        try:
            await hide_post(db, req.post_id, reason=req.reason)
            return ControlMutationResponse(
                ok=True, action="hide", scope="post", target_id=req.post_id,
            )
        finally:
            await db.close()

    @router.delete("/hide-post/{post_id}", response_model=ControlMutationResponse)
    async def unhide_post_route(board_id: str, post_id: str):
        """Lift a post hide."""
        db = await _open_subscribed_board_or_404(board_id)
        try:
            await unhide_post(db, post_id)
            return ControlMutationResponse(
                ok=True, action="unhide", scope="post", target_id=post_id,
            )
        finally:
            await db.close()

    # ── Post purge ───────────────────────────────────────────────────────────

    @router.post("/purge-post", response_model=PurgeResponse)
    async def purge_post_route(board_id: str, req: PostControlRequest):
        """Hard-delete a post and its payloads. Leaves a deny tombstone so the
        post is never re-admitted if a peer re-gossips it.

        If the post is a thread OP, the entire thread is purged (see
        purge.py for rationale — orphaned replies cannot be cleanly pruned).
        """
        db = await _open_subscribed_board_or_404(board_id)
        try:
            result = await purge_post(db, board_id, req.post_id, reason=req.reason)
            return PurgeResponse(
                ok=True,
                action="purge",
                scope=result.tombstoned_scope,
                target_id=result.tombstoned_target_id,
                deleted_posts=result.deleted_posts,
                deleted_payload_blobs=result.deleted_payload_blobs,
                deleted_chunk_caches=result.deleted_chunk_caches,
                deleted_manifests=result.deleted_manifests,
                deleted_sessions=result.deleted_sessions,
                deleted_request_states=result.deleted_request_states,
                deleted_availability_rows=result.deleted_availability_rows,
                purged_post_ids=result.purged_post_ids,
            )
        finally:
            await db.close()

    # ── Thread purge ─────────────────────────────────────────────────────────

    @router.post("/purge-thread", response_model=PurgeResponse)
    async def purge_thread_route(board_id: str, req: ThreadControlRequest):
        """Hard-delete an entire thread and all its payloads."""
        db = await _open_subscribed_board_or_404(board_id)
        try:
            result = await purge_thread(db, board_id, req.thread_id, reason=req.reason)
            return PurgeResponse(
                ok=True,
                action="purge",
                scope=result.tombstoned_scope,
                target_id=result.tombstoned_target_id,
                deleted_posts=result.deleted_posts,
                deleted_payload_blobs=result.deleted_payload_blobs,
                deleted_chunk_caches=result.deleted_chunk_caches,
                deleted_manifests=result.deleted_manifests,
                deleted_sessions=result.deleted_sessions,
                deleted_request_states=result.deleted_request_states,
                deleted_availability_rows=result.deleted_availability_rows,
                purged_post_ids=result.purged_post_ids,
            )
        finally:
            await db.close()

    # ── Unpurge (lift purge tombstone, allow network re-propagation) ─────────

    @router.delete("/purge-post/{post_id}", response_model=ControlMutationResponse)
    async def unpurge_post_route(board_id: str, post_id: str):
        """Lift a post purge tombstone.

        The metadata and payload were deleted by the original purge and are not
        restored by this call.  Removing the tombstone allows the post to be
        re-admitted via gossip once a peer re-propagates it.
        """
        db = await _open_subscribed_board_or_404(board_id)
        try:
            await unpurge_post(db, post_id)
            return ControlMutationResponse(
                ok=True, action="unpurge", scope="post", target_id=post_id,
            )
        finally:
            await db.close()

    @router.delete("/purge-thread/{thread_id}", response_model=ControlMutationResponse)
    async def unpurge_thread_route(board_id: str, thread_id: str):
        """Lift a thread purge tombstone.

        Same semantics as unpurge-post — the tombstone is cleared, content
        re-populates from the network via gossip.
        """
        db = await _open_subscribed_board_or_404(board_id)
        try:
            await unpurge_thread(db, thread_id)
            return ControlMutationResponse(
                ok=True, action="unpurge", scope="thread", target_id=thread_id,
            )
        finally:
            await db.close()

    # ── Identity hide / unhide ─────────────────────────────────────────────

    @router.post("/hide-identity", response_model=ControlMutationResponse)
    async def hide_identity_route(board_id: str, req: IdentityControlRequest):
        """Hide all posts from an identity. Posts move to the hidden bucket
        but are still shared with peers (local-only suppression)."""
        db = await _open_subscribed_board_or_404(board_id)
        try:
            await hide_identity(db, req.identity_hash, reason=req.reason)
            return ControlMutationResponse(
                ok=True, action="hide", scope="identity", target_id=req.identity_hash,
            )
        finally:
            await db.close()

    @router.delete("/hide-identity/{identity_hash}", response_model=ControlMutationResponse)
    async def unhide_identity_route(board_id: str, identity_hash: str):
        """Lift an identity hide."""
        db = await _open_subscribed_board_or_404(board_id)
        try:
            await unhide_identity(db, identity_hash)
            return ControlMutationResponse(
                ok=True, action="unhide", scope="identity", target_id=identity_hash,
            )
        finally:
            await db.close()

    # ── Identity ban / unban ────────────────────────────────────────────────

    @router.post("/ban-identity", response_model=IdentityBanResponse)
    async def ban_identity_route(board_id: str, req: IdentityControlRequest):
        """Ban an identity: block + cascade-purge all existing posts.

        Any existing hide control for this identity is cleared (ban supersedes).
        """
        db = await _open_subscribed_board_or_404(board_id)
        try:
            await block_identity(db, req.identity_hash, reason=req.reason)
            # Clear hide if present (ban supersedes hide).
            await unhide_identity(db, req.identity_hash)
            action_id, result = await purge_identity(
                db, board_id, req.identity_hash, reason=req.reason,
            )
            return IdentityBanResponse(
                ok=True,
                action="ban",
                identity_hash=req.identity_hash,
                action_id=action_id,
                deleted_posts=result.deleted_posts,
                deleted_payload_blobs=result.deleted_payload_blobs,
                deleted_chunk_caches=result.deleted_chunk_caches,
                deleted_manifests=result.deleted_manifests,
                purged_post_ids=result.purged_post_ids,
            )
        finally:
            await db.close()

    @router.delete("/ban-identity/{identity_hash}", response_model=ControlMutationResponse)
    async def unban_identity_route(board_id: str, identity_hash: str):
        """Unban an identity: lift block + reverse cascade (lift purge tombstones
        created by this specific ban). Triggers catchup for re-acquisition."""
        db = await _open_subscribed_board_or_404(board_id)
        try:
            await unblock_identity(db, identity_hash)
            action_id = await get_active_moderation_action(db, "identity_ban", identity_hash)
            if action_id is not None:
                await reverse_moderation_action(db, action_id)
            if sync_engine is not None:
                sync_engine.schedule_catchup(board_id)
            return ControlMutationResponse(
                ok=True, action="unban", scope="identity", target_id=identity_hash,
            )
        finally:
            await db.close()

    # ── Attachment ban / unban ──────────────────────────────────────────────

    @router.post("/ban-attachment", response_model=ControlMutationResponse)
    async def ban_attachment_route(board_id: str, req: AttachmentControlRequest):
        """Ban an attachment by content hash.

        Sets the deny rule, then immediately hard-deletes the attachment blob,
        its chunk cache, and any in-flight chunk transfer state.  The post
        metadata and text payload are retained so the thread remains readable.
        The attachment cannot be re-acquired after a ban — serving is blocked at
        the policy layer even if a peer re-gossips the same blob.

        This is intentionally stronger than a purge: no re-download path is
        offered because the content is considered potentially illegal.
        """
        db = await _open_subscribed_board_or_404(board_id)
        try:
            await ban_attachment(db, req.attachment_content_hash, reason=req.reason)
            blob_hashes = [req.attachment_content_hash]
            # Hard-delete the payload file, chunk cache, and all transfer state.
            # Same ordering as purge_post: collect → delete files → delete DB state.
            delete_payloads_bulk(board_id, blob_hashes)
            delete_chunk_cache_bulk(board_id, blob_hashes)
            await delete_chunk_transfer_state_for_blobs(db, blob_hashes)
            await cancel_pending_chunk_session(req.attachment_content_hash)
            return ControlMutationResponse(
                ok=True, action="ban", scope="attachment",
                target_id=req.attachment_content_hash,
            )
        finally:
            await db.close()

    @router.delete("/ban-attachment/{attachment_content_hash}", response_model=ControlMutationResponse)
    async def unban_attachment_route(board_id: str, attachment_content_hash: str):
        """Unban an attachment hash."""
        db = await _open_subscribed_board_or_404(board_id)
        try:
            await unban_attachment(db, attachment_content_hash)
            return ControlMutationResponse(
                ok=True, action="unban", scope="attachment",
                target_id=attachment_content_hash,
            )
        finally:
            await db.close()

    # ── Banned list (for Ban section UI) ────────────────────────────────────

    @router.get("/banned", response_model=BannedListResponse)
    async def get_banned_list_route(board_id: str):
        """Return all banned identities and attachments for the Ban section UI."""
        db = await _open_subscribed_board_or_404(board_id)
        try:
            data = await get_banned_list(db)
            return BannedListResponse(
                identities=[BannedItemResponse(**item) for item in data["identities"]],
                attachments=[BannedItemResponse(**item) for item in data["attachments"]],
            )
        finally:
            await db.close()

    # ── Request network catchup (trigger after unpurge) ──────────────────────

    @router.post("/request-catchup")
    async def request_catchup_route(board_id: str):
        """Ask known peers for a fresh HAVE so this node can re-acquire
        gossip it may have missed.  Called immediately after lifting a purge
        tombstone so the deleted content can propagate back from the network.

        Calls sync_engine.schedule_catchup() which sends lightweight HAVE_REQs
        to known peers (§7.1 Tier 2).  Best-effort: if there are no peers, or a
        catchup was sent recently (cooldown), the engine silently skips it.
        The node will still catch up on the next periodic HAVE cycle regardless.

        The backend remains content-blind: no metadata or payloads are returned.
        Re-admission happens via the normal gossip receiver path (§6.2).
        """
        if sync_engine is not None:
            # schedule_catchup is thread-safe (uses call_soon_threadsafe
            # internally) and no-ops gracefully if the event loop is not ready.
            sync_engine.schedule_catchup(board_id)
        return {"ok": True, "board_id": board_id}

    return router
