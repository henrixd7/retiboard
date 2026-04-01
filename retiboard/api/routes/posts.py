"""
Post management API routes.

Spec references:
    §6.1 — Post creation flow (steps 5-7 happen here: metadata + payload storage)
    §6.2 — Reception validation (PoW check, content_hash check, board flags)
    §11  — PoW verification on every inbound post
    §3.1 — Structural metadata only
    §3.2 — Encrypted payload stored as opaque .bin
    §8.2 — text_only boards reject has_attachments=true

Design invariants:
    - The backend NEVER receives key_material, plaintext, or the AES-GCM key.
    - The payload blob arrives as raw encrypted bytes from the frontend.
    - We verify: PoW, content_hash (SHA-256 of ciphertext), board flags.
    - We store: metadata in SQLite, blob in /payloads/<content_hash>.bin.
    - content_hash is over the ENCRYPTED blob, not plaintext.

Endpoints:
    POST /api/boards/{board_id}/posts       — Create a new post
    GET  /api/boards/{board_id}/posts       — List thread catalog (summaries)
    GET  /api/boards/{board_id}/threads/{thread_id} — Get full thread
    GET  /api/boards/{board_id}/payloads/{content_hash} — Fetch opaque blob
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import Response
from pydantic import BaseModel, Field

from retiboard.db.models import PostMetadata
from retiboard.db.database import (
    open_board_db,
    open_existing_board_db,
    load_board_config,
    insert_post,
    get_thread_posts,
    get_catalog,
    post_exists,
    get_declared_payload_size,
)
from retiboard.storage.payloads import (
    write_payload,
    read_payload,
    delete_payload,
)
from retiboard.crypto.pow import verify_pow, verify_content_hash
from retiboard.moderation.policy import should_serve_blob
from retiboard.transport import get_max_payload_size
from retiboard.sync.payload_fetch import (
    cancel_chunk_fetch,
    get_chunk_fetch_progress,
    pause_chunk_fetch,
    resume_chunk_fetch,
)


# =============================================================================
# Request/Response models
# =============================================================================

class CreatePostRequest(BaseModel):
    """
    Metadata for a new post.

    This is the §3.1 structural metadata — ZERO content.
    The encrypted payload is sent separately as a file upload.
    """
    post_id: str = Field(..., min_length=1, max_length=128)
    thread_id: str = Field(..., min_length=1, max_length=128)
    parent_id: str = Field(default="", max_length=128)
    timestamp: int = Field(...)
    bump_flag: bool = True
    content_hash: str = Field(..., min_length=64, max_length=64)
    payload_size: int = Field(..., ge=1)
    attachment_content_hash: str = Field(default="", max_length=64)
    attachment_payload_size: int = Field(default=0, ge=0)
    has_attachments: bool = False
    attachment_count: int = Field(default=0, ge=0)
    text_only: bool = False
    identity_hash: str = Field(default="", max_length=128)
    pow_nonce: str = Field(default="", max_length=128)
    public_key: str = Field(default="", max_length=512)
    encrypted_pings: list[str] = Field(default_factory=list)
    edit_signature: str = Field(default="", max_length=2048)


class PostResponse(BaseModel):
    """Structural metadata returned by the API."""
    post_id: str
    thread_id: str
    parent_id: str
    timestamp: int
    bump_flag: bool
    content_hash: str
    payload_size: int
    attachment_content_hash: str
    attachment_payload_size: int
    has_attachments: bool
    attachment_count: int
    text_only: bool
    identity_hash: str
    pow_nonce: str
    public_key: str
    encrypted_pings: list[str]
    edit_signature: str
    thread_last_activity: int
    is_abandoned: bool
    expiry_timestamp: int


class ThreadSummaryResponse(BaseModel):
    """Catalog entry for a thread."""
    thread_id: str
    op_post_id: str
    post_count: int
    latest_post_timestamp: int
    thread_last_activity: int
    has_attachments: bool
    text_only: bool
    op_content_hash: str
    op_payload_size: int
    op_attachment_content_hash: str
    op_attachment_payload_size: int
    op_attachment_count: int
    public_key: str
    op_identity_hash: str
    expiry_timestamp: int




class PayloadFetchControlResponse(BaseModel):
    ok: bool
    state: str

class PayloadFetchProgressResponse(BaseModel):
    board_id: str
    blob_hash: str
    session_id: str
    state: str
    blob_kind: str = "text"
    chunk_count: int
    stored_chunks: int
    requested_chunks: int
    active_requests: int
    peer_count: int
    available_peers: int
    cooled_down_peers: int
    percent_complete: int
    complete: bool
    resumed_from_persisted: bool = False
    last_error: str = ""
    updated_at: int


# =============================================================================
# Router factory
# =============================================================================

def create_posts_router(board_manager, sync_engine=None, identity=None) -> APIRouter:
    """
    Create the posts API router.

    Args:
        board_manager: The BoardManager instance.
        sync_engine: The SyncEngine instance for gossip broadcast (§7).
        identity: This node's RNS identity for identity-stamping (§12.3).

    Returns:
        Configured APIRouter.
    """
    router = APIRouter(prefix="/api/boards/{board_id}", tags=["posts"])

    async def _open_subscribed_board_or_404(board_id: str):
        try:
            return await open_existing_board_db(board_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Board not found")

    # -----------------------------------------------------------------
    # POST /api/boards/{board_id}/posts — Create a new post
    # -----------------------------------------------------------------
    @router.post("/posts", response_model=PostResponse, status_code=201)
    async def create_post(
        board_id: str,
        metadata: str = Form(...),
        payload: UploadFile = File(...),
        attachment_payload: Optional[UploadFile] = File(None),
    ):
        """
        Create a new post: validate, store metadata + payload(s).

        The request is multipart/form-data:
          - metadata: JSON string of CreatePostRequest
          - payload: raw encrypted TEXT .bin blob (always present)
          - attachment_payload: raw encrypted ATTACHMENT .bin blob (optional)

        Split-blob model: text and attachment payloads are separate encrypted blobs.
        """
        import json

        # 1. Parse metadata JSON.
        try:
            meta_dict = json.loads(metadata)
            
            # v3.6.3: Transport-layer identity stamping.
            # Locally created posts are stamped with this node's identity hash.
            # This is "read from the incoming network packet" (the API request)
            # and is unforgeable by the frontend.
            if identity is not None:
                meta_dict["identity_hash"] = identity.hexhash
            
            req = CreatePostRequest(**meta_dict)
        except (json.JSONDecodeError, Exception) as e:
            raise HTTPException(status_code=400, detail=f"Invalid metadata: {e}")

        # 2. Load board config.
        db = await open_board_db(board_id)
        try:
            board_config = await load_board_config(db)
        except Exception:
            await db.close()
            raise HTTPException(status_code=404, detail="Board not found")

        if board_config is None:
            await db.close()
            raise HTTPException(status_code=404, detail="Board not found")

        try:
            # 3. Board flag validation (§8.2).
            if board_config.text_only and req.has_attachments:
                raise HTTPException(
                    status_code=400,
                    detail="Board is text_only: posts with attachments are rejected (§8.2)",
                )

            attachments_declared = bool(
                req.has_attachments
                or req.attachment_content_hash
                or req.attachment_payload_size
            )
            attachment_uploaded = attachment_payload is not None
            max_payload_size = get_max_payload_size()

            if attachments_declared and not attachment_uploaded:
                raise HTTPException(
                    status_code=400,
                    detail="Attachment metadata declared but attachment_payload is missing",
                )

            if attachment_uploaded and not attachments_declared:
                raise HTTPException(
                    status_code=400,
                    detail="attachment_payload uploaded but metadata does not declare attachments",
                )

            if attachments_declared and not req.attachment_content_hash:
                raise HTTPException(
                    status_code=400,
                    detail="Posts with attachments must declare attachment_content_hash",
                )

            if attachments_declared and req.attachment_payload_size <= 0:
                raise HTTPException(
                    status_code=400,
                    detail="Posts with attachments must declare positive attachment_payload_size",
                )

            if attachments_declared and req.attachment_count <= 0:
                raise HTTPException(
                    status_code=400,
                    detail="Posts with attachments must declare positive attachment_count",
                )

            if not attachments_declared and req.attachment_count != 0:
                raise HTTPException(
                    status_code=400,
                    detail="attachment_count must be 0 when no attachments are declared",
                )

            if req.payload_size > max_payload_size:
                raise HTTPException(
                    status_code=413,
                    detail=f"Payload exceeds max encrypted payload size ({max_payload_size} bytes)",
                )

            if req.attachment_payload_size > max_payload_size:
                raise HTTPException(
                    status_code=413,
                    detail=f"Attachment payload exceeds max encrypted payload size ({max_payload_size} bytes)",
                )

            # 4. PoW verification (§11).
            meta_for_pow = {
                "post_id": req.post_id,
                "thread_id": req.thread_id,
                "parent_id": req.parent_id,
                "timestamp": req.timestamp,
                "bump_flag": req.bump_flag,
                "content_hash": req.content_hash,
                "payload_size": req.payload_size,
                "attachment_content_hash": req.attachment_content_hash,
                "attachment_payload_size": req.attachment_payload_size,
                "has_attachments": req.has_attachments,
                "attachment_count": req.attachment_count,
                "text_only": req.text_only,
                "identity_hash": req.identity_hash,
                "public_key": req.public_key,
                "encrypted_pings": req.encrypted_pings,
                "edit_signature": req.edit_signature,
            }

            if not verify_pow(meta_for_pow, req.pow_nonce, board_config.pow_difficulty):
                raise HTTPException(
                    status_code=400,
                    detail="PoW verification failed: invalid nonce for board difficulty "
                           f"{board_config.pow_difficulty}",
                )

            # 5. Read and verify payload blob.
            payload_data = await payload.read()

            if len(payload_data) > max_payload_size:
                raise HTTPException(
                    status_code=413,
                    detail=f"Payload exceeds max encrypted payload size ({max_payload_size} bytes)",
                )

            if len(payload_data) != req.payload_size:
                raise HTTPException(
                    status_code=400,
                    detail=f"Payload size mismatch: declared {req.payload_size}, "
                           f"received {len(payload_data)}",
                )

            if not verify_content_hash(payload_data, req.content_hash):
                raise HTTPException(
                    status_code=400,
                    detail="content_hash mismatch: SHA-256 of payload does not match "
                           "declared content_hash",
                )

            attachment_data = None
            if attachment_uploaded and req.attachment_content_hash:
                attachment_data = await attachment_payload.read()

                if len(attachment_data) > max_payload_size:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Attachment payload exceeds max encrypted payload size ({max_payload_size} bytes)",
                    )

                if len(attachment_data) != req.attachment_payload_size:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Attachment payload size mismatch: declared "
                        f"{req.attachment_payload_size}, received {len(attachment_data)}",
                    )

                if not verify_content_hash(attachment_data, req.attachment_content_hash):
                    raise HTTPException(
                        status_code=400,
                        detail="attachment_content_hash mismatch",
                    )

            # 6. Dedup check.
            if await post_exists(db, req.post_id):
                raise HTTPException(
                    status_code=409,
                    detail=f"Post {req.post_id} already exists",
                )

            # 7. Compute expiry timestamp.
            expiry = req.timestamp + board_config.default_ttl_seconds

            # For OPs, thread_last_activity = timestamp.
            is_op = req.post_id == req.thread_id
            thread_last_activity = req.timestamp if is_op else 0

            # 8. Build PostMetadata and store.
            post = PostMetadata(
                post_id=req.post_id,
                thread_id=req.thread_id,
                parent_id=req.parent_id,
                timestamp=req.timestamp,
                expiry_timestamp=expiry,
                bump_flag=req.bump_flag,
                content_hash=req.content_hash,
                payload_size=req.payload_size,
                attachment_content_hash=req.attachment_content_hash,
                attachment_payload_size=req.attachment_payload_size,
                has_attachments=req.has_attachments,
                attachment_count=req.attachment_count,
                text_only=req.text_only,
                identity_hash=req.identity_hash,
                pow_nonce=req.pow_nonce,
                public_key=req.public_key,
                encrypted_pings=req.encrypted_pings,
                edit_signature=req.edit_signature,
                thread_last_activity=thread_last_activity,
                is_abandoned=False,
            )

            written_hashes: list[str] = []
            try:
                await insert_post(
                    db,
                    post,
                    thread_start_ttl=board_config.default_ttl_seconds,
                    thread_bump_ttl=board_config.bump_decay_rate,
                    commit=False,
                )

                # 9. Store opaque payload(s) only after full validation.
                write_payload(board_id, req.content_hash, payload_data, verify_hash=False)
                written_hashes.append(req.content_hash)

                if attachment_data is not None and req.attachment_content_hash:
                    write_payload(
                        board_id,
                        req.attachment_content_hash,
                        attachment_data,
                        verify_hash=False,
                    )
                    written_hashes.append(req.attachment_content_hash)

                await db.commit()
            except HTTPException:
                await db.rollback()
                for blob_hash in reversed(written_hashes):
                    delete_payload(board_id, blob_hash)
                raise
            except Exception as exc:
                await db.rollback()
                for blob_hash in reversed(written_hashes):
                    delete_payload(board_id, blob_hash)
                raise HTTPException(status_code=500, detail=f"Failed to store post payloads: {exc}")

            # 10. Trigger gossip broadcast (§7.1 Tier 1).
            if sync_engine is not None:
                sync_engine.on_local_post_created(post, board_id)

            # 11. Push to connected WebSocket clients (§10 real-time updates).
            from retiboard.api.routes.sync import ws_manager
            await ws_manager.broadcast_to_board(
                board_id, "new_post", post.to_dict(),
            )

            # Return the stored metadata.
            return _post_to_response(post)

        finally:
            await db.close()

    # -----------------------------------------------------------------
    # GET /api/boards/{board_id}/posts — Thread catalog
    # -----------------------------------------------------------------
    @router.get("/posts", response_model=list[ThreadSummaryResponse])
    async def list_threads(board_id: str, limit: int = 50):
        """
        Get the board catalog: active threads sorted by bump order.
        """
        db = await _open_subscribed_board_or_404(board_id)
        try:
            catalog = await get_catalog(db, limit=limit)
            return [
                ThreadSummaryResponse(
                    thread_id=t.thread_id,
                    op_post_id=t.op_post_id,
                    post_count=t.post_count,
                    latest_post_timestamp=t.latest_post_timestamp,
                    thread_last_activity=t.thread_last_activity,
                    has_attachments=t.has_attachments,
                    text_only=t.text_only,
                    op_content_hash=t.op_content_hash,
                    op_payload_size=t.op_payload_size,
                    op_attachment_content_hash=t.op_attachment_content_hash,
                    op_attachment_payload_size=t.op_attachment_payload_size,
                    op_attachment_count=t.op_attachment_count,
                    public_key=t.public_key,
                    op_identity_hash=t.op_identity_hash,
                    expiry_timestamp=t.expiry_timestamp,
                )
                for t in catalog
            ]
        finally:
            await db.close()

    # -----------------------------------------------------------------
    # GET /api/boards/{board_id}/threads/{thread_id} — Full thread
    # -----------------------------------------------------------------
    @router.get(
        "/threads/{thread_id}",
        response_model=list[PostResponse],
    )
    async def get_thread(board_id: str, thread_id: str):
        """
        Get all posts in a thread, ordered chronologically.
        """
        db = await _open_subscribed_board_or_404(board_id)
        try:
            posts = await get_thread_posts(db, thread_id)
            if not posts:
                raise HTTPException(status_code=404, detail="Thread not found")
            return [_post_to_response(p) for p in posts]
        finally:
            await db.close()

    # -----------------------------------------------------------------
    # GET /api/boards/{board_id}/payloads/{content_hash}/progress — Fetch progress
    # -----------------------------------------------------------------
    @router.get("/payloads/{content_hash}/progress", response_model=PayloadFetchProgressResponse)
    async def get_payload_progress(board_id: str, content_hash: str):
        progress = await get_chunk_fetch_progress(board_id, content_hash)
        if progress is None:
            raise HTTPException(status_code=404, detail="No payload fetch session")
        return PayloadFetchProgressResponse(**progress)

    # -----------------------------------------------------------------
    # POST /api/boards/{board_id}/payloads/{content_hash}/pause — Pause fetch
    # -----------------------------------------------------------------
    @router.post("/payloads/{content_hash}/pause", response_model=PayloadFetchControlResponse)
    async def pause_payload_fetch_route(board_id: str, content_hash: str):
        ok = await pause_chunk_fetch(board_id, content_hash, sync_engine=sync_engine)
        if not ok:
            raise HTTPException(status_code=404, detail="No payload fetch session")
        return PayloadFetchControlResponse(ok=True, state="paused")

    # -----------------------------------------------------------------
    # POST /api/boards/{board_id}/payloads/{content_hash}/resume — Resume fetch
    # -----------------------------------------------------------------
    @router.post("/payloads/{content_hash}/resume", response_model=PayloadFetchControlResponse)
    async def resume_payload_fetch_route(board_id: str, content_hash: str):
        ok = await resume_chunk_fetch(board_id, content_hash)
        if not ok:
            raise HTTPException(status_code=404, detail="No persisted payload fetch session")
        return PayloadFetchControlResponse(ok=True, state="started")

    # -----------------------------------------------------------------
    # DELETE /api/boards/{board_id}/payloads/{content_hash}/fetch — Cancel fetch
    # -----------------------------------------------------------------
    @router.delete("/payloads/{content_hash}/fetch", response_model=PayloadFetchControlResponse)
    async def cancel_payload_fetch_route(board_id: str, content_hash: str):
        ok = await cancel_chunk_fetch(board_id, content_hash, sync_engine=sync_engine)
        if not ok:
            raise HTTPException(status_code=404, detail="No payload fetch session")
        return PayloadFetchControlResponse(ok=True, state="cancelled")

    # -----------------------------------------------------------------
    # GET /api/boards/{board_id}/payloads/{content_hash} — Fetch blob
    # -----------------------------------------------------------------
    @router.get("/payloads/{content_hash}")
    async def get_payload(board_id: str, content_hash: str, manual: bool = False):
        """
        Fetch an opaque encrypted payload blob.

        Returns raw bytes with application/octet-stream.
        No MIME sniffing, no thumbnailing, no transcoding.
        The frontend (and ONLY the frontend) decrypts these.

        If the payload is missing locally, attempts to fetch it from
        peers on-demand (§7.1: "Payloads are always fetched on-demand
        when the frontend first renders a post").
        """
        db = await _open_subscribed_board_or_404(board_id)
        try:
            decision = await should_serve_blob(db, content_hash)
            if not decision.allowed:
                status = 404 if decision.reason == "not_found" else 403
                raise HTTPException(status_code=status, detail=f"Payload unavailable: {decision.reason}")
            data = read_payload(board_id, content_hash)

            # On-demand fetch from peers if missing locally (§7.1 Tier 3).
            if data is None and sync_engine is not None:
                expected_size = await get_declared_payload_size(db, content_hash)
                fetched = await sync_engine.fetch_payload(
                    board_id,
                    content_hash,
                    expected_size=expected_size,
                    manual_override=bool(manual),
                )
                if fetched:
                    data = read_payload(board_id, content_hash)

            if data is None:
                raise HTTPException(status_code=404, detail="Payload not found")
        finally:
            await db.close()

        return Response(
            content=data,
            media_type="application/octet-stream",
            headers={
                "Content-Length": str(len(data)),
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
            },
        )

    return router


def _post_to_response(post: PostMetadata) -> PostResponse:
    """Convert a PostMetadata to an API response."""
    return PostResponse(
        post_id=post.post_id,
        thread_id=post.thread_id,
        parent_id=post.parent_id,
        timestamp=post.timestamp,
        bump_flag=post.bump_flag,
        content_hash=post.content_hash,
        payload_size=post.payload_size,
        attachment_content_hash=post.attachment_content_hash,
        attachment_payload_size=post.attachment_payload_size,
        has_attachments=post.has_attachments,
        attachment_count=post.attachment_count,
        text_only=post.text_only,
        identity_hash=post.identity_hash,
        pow_nonce=post.pow_nonce,
        public_key=post.public_key,
        encrypted_pings=post.encrypted_pings,
        edit_signature=post.edit_signature,
        thread_last_activity=post.thread_last_activity,
        is_abandoned=post.is_abandoned,
        expiry_timestamp=post.expiry_timestamp,
    )
