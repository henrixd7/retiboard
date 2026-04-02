"""
Tier 1 — LXMF message receiver for incoming post metadata.

Spec references:
    §6.2 — Reception validation: PoW, board flags, content_hash
    §7.1 — "On receiving metadata: Validate PoW, validate against board flags,
           store in meta.db, update thread state."

This is the LXMF delivery callback. When a peer broadcasts post metadata,
the LXMRouter calls this handler.

Validation steps (§6.2):
    1. Parse JSON metadata from LXMF message content.
    2. Identify the board (from _board_id field in metadata).
    3. Load board config and verify board exists locally.
    4. Validate PoW against board difficulty.
    5. Validate board flags (e.g., reject has_attachments on text_only board).
    6. Check for duplicates (post_id already exists).
    7. Store metadata in meta.db.
    8. Enqueue payload fetch (on-demand, not immediate — §7.1).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Optional, TYPE_CHECKING

import RNS

from retiboard.sync import (
    MSG_TYPE_METADATA,
    MSG_TYPE_HAVE,
    MSG_TYPE_HAVE_REQ,
    MSG_TYPE_DELTA_REQ,
    MSG_TYPE_DELTA_RES,
    MSG_TYPE_PAYLOAD_REQ,
    MSG_TYPE_PAYLOAD_RES,
    MSG_TYPE_BOARD_ANNOUNCE,
    MSG_TYPE_CHUNK_MANIFEST_REQ,
    MSG_TYPE_CHUNK_MANIFEST_RES,
    MSG_TYPE_CHUNK_MANIFEST_UNAV,
    MSG_TYPE_CHUNK_REQ,
    MSG_TYPE_CHUNK_CANCEL,
    MSG_TYPE_CHUNK_OFFER,
    MSG_TYPE_BOARD_LIST_REQ,
    MSG_TYPE_BOARD_LIST_RES,
)
from retiboard.db.models import PostMetadata
from retiboard.crypto.pow import verify_pow
from retiboard.moderation.policy import should_reject_post
from retiboard.transport import get_max_payload_size

if TYPE_CHECKING:
    from retiboard.sync.peers import PeerTracker


def _schedule_on_engine_loop(sync_engine, coro, context: str) -> bool:
    loop = getattr(sync_engine, "_loop", None)
    if isinstance(loop, asyncio.AbstractEventLoop) and not loop.is_closed():
        def _schedule():
            loop.create_task(coro)
        loop.call_soon_threadsafe(_schedule)
        return True

    try:
        asyncio.get_running_loop().create_task(coro)
        return True
    except RuntimeError:
        pass

    try:
        coro.close()
    except Exception:
        pass
    RNS.log(f"{context}: engine loop unavailable; dropping async task", RNS.LOG_WARNING)
    return False


def _resolve_source_identity(message, peer_tracker: Optional["PeerTracker"] = None):
    """Resolve the sender identity for an incoming LXMF message.

    Tier 3 payload/chunk serving depends on the requester's identity to derive
    the dedicated ``retiboard.payload`` destination. Fresh installs may know a
    peer identity via board announce tracking before ``RNS.Identity.recall()``
    is warm, so fall back to the peer tracker instead of relying on the RNS
    recall cache alone.
    """
    source_destination = getattr(message, "source", None)
    source_identity = getattr(source_destination, "identity", None)
    if source_identity is not None:
        return source_identity

    source_hash = getattr(message, "source_hash", None)
    if source_hash:
        try:
            source_identity = RNS.Identity.recall(source_hash)
        except Exception:
            source_identity = None
        if source_identity is not None:
            return source_identity

        if peer_tracker is not None:
            source_hex = source_hash.hex() if isinstance(source_hash, bytes) else str(source_hash)
            peer = peer_tracker.get_peer(source_hex)
            if peer is not None and peer.identity is not None:
                RNS.log(
                    f"Recovered sender identity for {source_hex[:16]} from peer tracker",
                    RNS.LOG_DEBUG,
                )
                return peer.identity

    return None


async def handle_incoming_metadata(
    meta_dict: dict,
    board_id: str,
    source_hash: Optional[bytes] = None,
    source_identity: Optional[RNS.Identity] = None,
    peer_tracker: Optional["PeerTracker"] = None,
    sync_engine=None,
) -> bool:
    """
    Process incoming post metadata from a remote peer.

    This is called when we receive metadata via LXMF (Tier 1) or
    DELTA_RESPONSE (Tier 3). The validation logic is shared.

    Args:
        meta_dict: The §3.1 metadata dict.
        board_id: Board this metadata belongs to.
        source_hash: Source peer's destination hash (for peer tracking).
        source_identity: Source peer's RNS Identity (for link establishment).
        peer_tracker: Peer tracker to update.

    Returns:
        True if the metadata was stored, False if rejected/duplicate.
    """
    from retiboard.db.database import (
        open_board_db, load_board_config, insert_post, post_exists,
        is_board_subscribed,
    )

    post_id = meta_dict.get("post_id", "")
    if not post_id:
        RNS.log("Rejected metadata: missing post_id", RNS.LOG_DEBUG)
        return False

    # Gate: don't process metadata for boards we're not subscribed to.
    # open_board_db auto-creates directories, which would resurrect ghost
    # boards after unsubscribe.
    if not is_board_subscribed(board_id):
        RNS.log(
            f"Rejected metadata for unsubscribed board {board_id[:8]}",
            RNS.LOG_DEBUG,
        )
        return False

    db = await open_board_db(board_id)
    try:
        # 1. Load board config.
        config = await load_board_config(db)
        if config is None:
            RNS.log(
                f"Rejected metadata for unknown board {board_id[:8]}",
                RNS.LOG_DEBUG,
            )
            return False

        # 2. Check for duplicates.
        if await post_exists(db, post_id):
            RNS.log(
                f"Duplicate post {post_id[:12]}, skipping",
                RNS.LOG_DEBUG,
            )
            return False

        # v3.6.3: Transport-layer identity stamping.
        # If the post has no identity_hash, stamp it with the sender's identity
        # from the network packet (source_hash). This makes identity mandatory
        # and unforgeable for moderation.
        if not meta_dict.get("identity_hash") and source_hash:
            source_hex = source_hash.hex() if isinstance(source_hash, bytes) else source_hash
            meta_dict["identity_hash"] = source_hex
            RNS.log(
                f"Stamped post {post_id[:12]} with transport identity {source_hex[:16]}",
                RNS.LOG_DEBUG,
            )

        # 3. Validate PoW (§11).
        pow_fields = {
            "post_id": meta_dict.get("post_id", ""),
            "thread_id": meta_dict.get("thread_id", ""),
            "parent_id": meta_dict.get("parent_id", ""),
            "timestamp": meta_dict.get("timestamp", 0),
            "bump_flag": meta_dict.get("bump_flag", False),
            "content_hash": meta_dict.get("content_hash", ""),
            "payload_size": meta_dict.get("payload_size", 0),
            "attachment_content_hash": meta_dict.get("attachment_content_hash", ""),
            "attachment_payload_size": meta_dict.get("attachment_payload_size", 0),
            "has_attachments": meta_dict.get("has_attachments", False),
            "attachment_count": meta_dict.get("attachment_count", 0),
            "text_only": meta_dict.get("text_only", False),
            "identity_hash": meta_dict.get("identity_hash", ""),
            "public_key": meta_dict.get("public_key", ""),
            "encrypted_pings": meta_dict.get("encrypted_pings", []),
            "edit_signature": meta_dict.get("edit_signature", ""),
        }

        pow_nonce = meta_dict.get("pow_nonce", "")
        if not verify_pow(pow_fields, pow_nonce, config.pow_difficulty):
            RNS.log(
                f"Rejected post {post_id[:12]}: invalid PoW",
                RNS.LOG_WARNING,
            )
            return False

        # 4. Validate board flags (§8.2).
        if config.text_only and meta_dict.get("has_attachments", False):
            RNS.log(
                f"Rejected post {post_id[:12]}: attachments on text_only board",
                RNS.LOG_WARNING,
            )
            return False

        # v3.6.3: Adversarial hardening — Timestamp sanity checks (§4).
        # Prevent replay of very old posts that have already expired.
        # Prevent "future posts" that would sit at the top of catalog indefinitely.
        now = int(time.time())
        wire_ts = int(meta_dict.get("timestamp", 0))
        
        # Max age: must not be older than the board's default TTL.
        if wire_ts < (now - config.default_ttl_seconds):
            RNS.log(
                f"Rejected post {post_id[:12]}: timestamp too old ({now - wire_ts}s ago)",
                RNS.LOG_WARNING,
            )
            return False
            
        # Max future: allow 1 hour for clock skew.
        if wire_ts > (now + 3600):
            RNS.log(
                f"Rejected post {post_id[:12]}: timestamp too far in future",
                RNS.LOG_WARNING,
            )
            return False

        try:
            payload_size = int(meta_dict.get("payload_size", 0) or 0)
            attachment_payload_size = int(meta_dict.get("attachment_payload_size", 0) or 0)
            attachment_count = int(meta_dict.get("attachment_count", 0) or 0)
        except (TypeError, ValueError):
            RNS.log(
                f"Rejected post {post_id[:12]}: malformed payload sizing fields",
                RNS.LOG_WARNING,
            )
            return False

        attachments_declared = bool(
            meta_dict.get("has_attachments", False)
            or meta_dict.get("attachment_content_hash", "")
            or attachment_payload_size
        )
        max_payload_size = get_max_payload_size()

        if payload_size <= 0:
            RNS.log(
                f"Rejected post {post_id[:12]}: invalid payload_size",
                RNS.LOG_WARNING,
            )
            return False

        if attachments_declared and attachment_count <= 0:
            RNS.log(
                f"Rejected post {post_id[:12]}: invalid attachment_count",
                RNS.LOG_WARNING,
            )
            return False

        if not attachments_declared and attachment_count != 0:
            RNS.log(
                f"Rejected post {post_id[:12]}: attachment_count without attachments",
                RNS.LOG_WARNING,
            )
            return False

        if attachments_declared and attachment_payload_size <= 0:
            RNS.log(
                f"Rejected post {post_id[:12]}: invalid attachment_payload_size",
                RNS.LOG_WARNING,
            )
            return False

        if payload_size > max_payload_size:
            RNS.log(
                f"Rejected post {post_id[:12]}: text payload exceeds local max",
                RNS.LOG_WARNING,
            )
            return False

        if attachment_payload_size > max_payload_size:
            RNS.log(
                f"Rejected post {post_id[:12]}: attachment payload exceeds local max",
                RNS.LOG_WARNING,
            )
            return False

        # 5. Build PostMetadata and store.
        timestamp = meta_dict.get("timestamp", int(time.time()))
        is_op = meta_dict.get("post_id") == meta_dict.get("thread_id")

        # thread_last_activity: propagate from wire, don't recompute to 0.
        # The sender denormalises the thread's latest bump timestamp onto
        # the OP row for cheap HAVE/catalog queries. For OP posts we take
        # max(wire, timestamp) as a clock-skew guard. For non-OP posts we
        # honour the wire value; insert_post() will recompute the full
        # thread lifecycle once the OP is present locally.
        wire_tla = meta_dict.get("thread_last_activity", 0)
        if is_op:
            thread_last_activity = max(wire_tla, timestamp)
        else:
            # Non-OP rows legitimately carry 0; insert_post's UPDATE
            # corrects the OP row when a bump_flag=True reply is stored.
            thread_last_activity = wire_tla

        post = PostMetadata(
            post_id=meta_dict["post_id"],
            thread_id=meta_dict["thread_id"],
            parent_id=meta_dict.get("parent_id", ""),
            timestamp=timestamp,
            expiry_timestamp=timestamp + config.default_ttl_seconds,
            bump_flag=meta_dict.get("bump_flag", False),
            content_hash=meta_dict["content_hash"],
            payload_size=payload_size,
            attachment_content_hash=meta_dict.get("attachment_content_hash", ""),
            attachment_payload_size=attachment_payload_size,
            has_attachments=meta_dict.get("has_attachments", False),
            attachment_count=attachment_count,
            text_only=meta_dict.get("text_only", False),
            identity_hash=meta_dict.get("identity_hash", ""),
            pow_nonce=pow_nonce,
            public_key=meta_dict.get("public_key", ""),
            encrypted_pings=(
                [item for item in meta_dict.get("encrypted_pings", []) if isinstance(item, str)]
                if isinstance(meta_dict.get("encrypted_pings", []), list)
                else []
            ),
            edit_signature=meta_dict.get("edit_signature", ""),
            thread_last_activity=thread_last_activity,
            is_abandoned=False,
        )

        decision = await should_reject_post(db, post)
        if not decision.allowed:
            RNS.log(
                f"Rejected post {post_id[:12]}: moderation {decision.reason}",
                RNS.LOG_DEBUG,
            )
            return False

        await insert_post(
            db,
            post,
            thread_start_ttl=config.default_ttl_seconds,
            thread_bump_ttl=config.bump_decay_rate,
        )

        try:
            from retiboard.api.routes.sync import ws_manager
            await ws_manager.broadcast_to_board(
                board_id, "new_post", post.to_dict(),
            )
        except Exception as exc:
            RNS.log(
                f"Failed to broadcast remote post {post_id[:12]} to WebSocket clients: {exc}",
                RNS.LOG_DEBUG,
            )

        RNS.log(
            f"Stored remote post {post_id[:12]} for board {board_id[:8]}",
            RNS.LOG_DEBUG,
        )

        # 6. Opportunistic replication (§7.1).
        # Forward this metadata to a few other peers to help it propagate.
        if sync_engine and sync_engine._lxm_router and sync_engine._lxmf_destination:
            from retiboard.sync.replication import replicate_metadata
            asyncio.create_task(replicate_metadata(
                sync_engine._lxm_router,
                sync_engine._lxmf_destination,
                post,
                board_id,
                peer_tracker,
                exclude_source=source_hash,
                sync_engine=sync_engine,
            ))

        # 7. Track the source peer.
        # v3.6.2 §9.2: message.source is authoritative → register_from_message.
        # We pass the identity so the peer can be used for link-based
        # operations (payload fetch, delta requests). Without the identity,
        # the peer would be invisible to get_lxmf_peers() / get_fetch_peers().
        if peer_tracker and source_hash:
            source_hex = source_hash.hex() if isinstance(source_hash, bytes) else source_hash

            # If no identity was passed, try to recall it from RNS.
            # After receiving an LXMF message, RNS has the sender's
            # identity cached from the link establishment.
            identity = source_identity
            if identity is None:
                try:
                    identity = RNS.Identity.recall(source_hash)
                    if identity:
                        RNS.log(
                            f"Recalled identity for peer {source_hex[:16]}",
                            RNS.LOG_DEBUG,
                        )
                except Exception:
                    pass

            peer_tracker.register_from_message(
                source_hex, board_id=board_id, identity=identity,
            )

        return True

    except Exception as e:
        RNS.log(
            f"Error processing metadata for {post_id[:12]}: {e}",
            RNS.LOG_WARNING,
        )
        return False
    finally:
        await db.close()


def make_delivery_callback(peer_tracker: "PeerTracker", sync_engine=None):
    """
    Create an LXMF delivery callback for incoming messages.

    The callback is registered with LXMRouter.register_delivery_callback().
    It dispatches based on the message title (which serves as a type tag).

    Handles:
        MSG_TYPE_METADATA      — Tier 1: incoming post metadata
        MSG_TYPE_HAVE          — Tier 2: HAVE announcement
        MSG_TYPE_HAVE_REQ      — Tier 2: catch-up HAVE request (responds with HAVE)
        MSG_TYPE_DELTA_REQ     — Tier 3: delta request (responds with DELTA_RES)
        MSG_TYPE_DELTA_RES     — Tier 3: delta response (stores metadata)
        MSG_TYPE_PAYLOAD_REQ   — Tier 3: payload request (v3.6.2)
        MSG_TYPE_PAYLOAD_RES   — Tier 3: payload response (v3.6.2)
        MSG_TYPE_BOARD_ANNOUNCE — Board announce push (cold-start race fix)

    RNS/LXMF API (verified):
        Callback signature: callback(message)
        message.content — bytes (the message content)
        message.title — bytes (our message type tag)
        message.source_hash — bytes (sender's identity hash)
        message.signature_validated — bool
        message.timestamp — float

    Args:
        peer_tracker: Peer tracker for registering message sources.
        sync_engine: SyncEngine for sending LXMF responses (needed for
                     PAYLOAD_REQ handling — the responder must send back
                     a PAYLOAD_RES via LXMF).
    """

    def delivery_callback(message):
        """LXMF delivery callback — dispatches by message type."""
        try:
            # Decode title to get message type.
            title = message.title.decode("utf-8") if message.title else ""
            content = message.content.decode("utf-8") if message.content else ""

            source_identity = _resolve_source_identity(message, peer_tracker)

            if title == MSG_TYPE_METADATA:
                # Tier 1: incoming post metadata.
                try:
                    meta_dict = json.loads(content)
                except json.JSONDecodeError:
                    RNS.log("Invalid JSON in metadata message", RNS.LOG_WARNING)
                    return

                board_id = meta_dict.pop("_board_id", None)
                if not board_id:
                    RNS.log("Metadata message missing _board_id", RNS.LOG_WARNING)
                    return

                # Run async handler on the engine loop.
                _schedule_on_engine_loop(
                    sync_engine,
                    handle_incoming_metadata(
                        meta_dict, board_id,
                        source_hash=message.source_hash,
                        source_identity=source_identity,
                        peer_tracker=peer_tracker,
                        sync_engine=sync_engine,
                    ),
                    "MSG_TYPE_METADATA",
                )

            elif title == MSG_TYPE_HAVE:
                # Tier 2: HAVE announcement received via LXMF.
                # v3.6.2 §13.1: LXMF direct is the primary HAVE delivery path
                # for known peers. Process it the same way as announce-based HAVE,
                # but with is_from_board_announce=False since source_hash here IS
                # the peer's LXMF hash (authoritative per §9.3).
                RNS.log(
                    f"Received HAVE via LXMF from "
                    f"{message.source_hash.hex()[:16] if message.source_hash else 'unknown'}",
                    RNS.LOG_DEBUG,
                )

                # Register peer authoritatively (§9.2: message.source is authoritative).
                if peer_tracker and message.source_hash:
                    source_hex = message.source_hash.hex()
                    peer_tracker.register_from_message(
                        source_hex, identity=source_identity,
                    )

                from retiboard.sync.have_handler import handle_have_announcement
                have_bytes = content.encode("utf-8") if isinstance(content, str) else content

                # Schedule on the engine's event loop so that any delta
                # requests enqueued by handle_have_announcement land on
                # the correct asyncio.Queue (owned by the main loop).
                _schedule_on_engine_loop(
                    sync_engine,
                    handle_have_announcement(
                        have_bytes,
                        source_hash=message.source_hash,
                        source_identity=source_identity,
                        peer_tracker=peer_tracker,
                        is_from_board_announce=False,
                    ),
                    "MSG_TYPE_HAVE",
                )

            elif title == MSG_TYPE_PAYLOAD_REQ:
                # Tier 3 (v3.6.2): Payload request received via LXMF.
                # Read the payload locally and send it back as LXMF response.
                RNS.log(
                    f"Received PAYLOAD_REQUEST via LXMF from "
                    f"{message.source_hash.hex()[:16] if message.source_hash else 'unknown'}",
                    RNS.LOG_DEBUG,
                )
                from retiboard.sync.payload_fetch import handle_payload_request_lxmf

                # Register the peer (authoritative — they sent us an LXMF msg).
                if peer_tracker and message.source_hash:
                    source_hex = message.source_hash.hex()
                    peer_tracker.register_from_message(
                        source_hex, identity=source_identity,
                    )

                _schedule_on_engine_loop(
                    sync_engine,
                    handle_payload_request_lxmf(
                        content,
                        message.source_hash,
                        source_identity,
                        sync_engine,
                    ),
                    "MSG_TYPE_PAYLOAD_REQ",
                )

            elif title == MSG_TYPE_HAVE_REQ:
                # Tier 2 catch-up: peer is requesting our HAVE for a board.
                # Triggered when a peer subscribes to a board or discovers us
                # and wants to immediately sync rather than wait for the next
                # periodic HAVE cycle (§7.1 Tier 2, §13.1).
                #
                # We build our current HAVE for the requested board and send
                # it back via MSG_TYPE_HAVE — the requester processes it via
                # the standard HAVE handler, which compares and enqueues
                # DELTA_REQUESTs as needed.
                RNS.log(
                    f"Received HAVE_REQ via LXMF from "
                    f"{message.source_hash.hex()[:16] if message.source_hash else 'unknown'}",
                    RNS.LOG_DEBUG,
                )

                # Register peer authoritatively (§9.2).
                if peer_tracker and message.source_hash:
                    source_hex = message.source_hash.hex()
                    peer_tracker.register_from_message(
                        source_hex, identity=source_identity,
                    )

                async def _handle_have_req(req_content, requester_hash):
                    """Async handler: build HAVE and send back via LXMF."""
                    try:
                        req = json.loads(req_content)
                        board_id = req.get("board_id")
                        if not board_id:
                            RNS.log("HAVE_REQ missing board_id", RNS.LOG_DEBUG)
                            return

                        # Verify we actually have this board locally.
                        from retiboard.config import BOARDS_DIR
                        board_path = BOARDS_DIR / board_id
                        if not board_path.exists() or not (board_path / "meta.db").exists():
                            RNS.log(
                                f"HAVE_REQ for unknown board {board_id[:8]}, ignoring",
                                RNS.LOG_DEBUG,
                            )
                            return

                        from retiboard.sync.have import build_have_packet, serialize_have
                        from retiboard.transport import is_low_bandwidth
                        have = await build_have_packet(board_id, is_low_bandwidth=is_low_bandwidth())
                        if have is None:
                            RNS.log(
                                f"HAVE_REQ: no active threads for board {board_id[:8]}",
                                RNS.LOG_DEBUG,
                            )
                            return

                        have_bytes = serialize_have(have)

                        # Send our HAVE back to the requester via LXMF.
                        if sync_engine and requester_hash:
                            from retiboard.sync.message_queue import Priority
                            requester_hex = requester_hash.hex() if isinstance(requester_hash, bytes) else requester_hash
                            sync_engine.send_lxmf(
                                requester_hex,
                                have_bytes,
                                MSG_TYPE_HAVE,
                                Priority.CONTROL,
                            )
                            RNS.log(
                                f"HAVE_REQ: sent HAVE response for board {board_id[:8]} "
                                f"({len(have.get('active_threads', []))} threads) "
                                f"to {requester_hex[:16]}",
                                RNS.LOG_DEBUG,
                            )
                    except Exception as e:
                        RNS.log(f"HAVE_REQ handler error: {e}", RNS.LOG_DEBUG)

                # Schedule on the engine's event loop for correct asyncio.Queue
                # interaction (delta requests from the HAVE response).
                _schedule_on_engine_loop(
                    sync_engine,
                    _handle_have_req(content, message.source_hash),
                    "MSG_TYPE_HAVE_REQ",
                )

            elif title == MSG_TYPE_DELTA_REQ:
                # ============================================================
                # Tier 3 DELTA_REQUEST via LXMF.
                #
                # ARCHITECTURE NOTE: Delta sync was originally implemented
                # using RNS Link.request() (synchronous request/response over
                # an established link).  This NEVER worked reliably because:
                #   - RNS request handlers run in the transport thread
                #   - build_delta_response() needs async DB access
                #   - Blocking the handler thread (asyncio.run, ThreadPool,
                #     etc.) causes link teardown before the response can be
                #     transmitted: "Attempt to transmit over a closed link"
                #   - Exponential backoff accumulates, making sync impossible
                #
                # FIX: Use LXMF messages (same pattern as HAVE_REQ/HAVE).
                # Peer sends MSG_TYPE_DELTA_REQ, we build the response async,
                # and send MSG_TYPE_DELTA_RES back via LXMF.  No link
                # management, no thread blocking, works through propagation.
                # trigger payload fetches.
                # ============================================================
                RNS.log(
                    f"Received DELTA_RES via LXMF from "
                    f"{message.source_hash.hex()[:16] if message.source_hash else 'unknown'}",
                    RNS.LOG_DEBUG,
                )

                # Register peer authoritatively (§9.2).
                if peer_tracker and message.source_hash:
                    source_hex = message.source_hash.hex()
                    peer_tracker.register_from_message(
                        source_hex, identity=source_identity,
                    )

                async def _handle_delta_req(req_content, requester_hash):
                    """Async handler: build delta response and send back via LXMF."""
                    try:
                        req = json.loads(req_content)
                        board_id = req.get("board_id", "")
                        thread_id = req.get("thread_id", "")
                        since_timestamp = req.get("since_timestamp", 0)
                        known_post_count = req.get("known_post_count", 0)

                        if not board_id or not thread_id:
                            RNS.log("DELTA_REQ missing fields", RNS.LOG_DEBUG)
                            return

                        from retiboard.db.database import is_board_subscribed
                        if not is_board_subscribed(board_id):
                            RNS.log(
                                f"DELTA_REQ for unsubscribed board {board_id[:8]}",
                                RNS.LOG_DEBUG,
                            )
                            return

                        from retiboard.sync.delta import build_delta_response
                        response = await build_delta_response(
                            board_id, thread_id, since_timestamp, known_post_count,
                        )

                        if not response.get("metadata"):
                            RNS.log(
                                f"DELTA_REQ: no new records for thread {thread_id[:12]}",
                                RNS.LOG_DEBUG,
                            )
                            return

                        response_bytes = json.dumps(
                            response, separators=(",", ":")
                        ).encode("utf-8")

                        # Send delta response back to requester via LXMF.
                        if sync_engine and requester_hash:
                            from retiboard.sync.message_queue import Priority
                            requester_hex = (
                                requester_hash.hex()
                                if isinstance(requester_hash, bytes)
                                else requester_hash
                            )
                            sync_engine.send_lxmf(
                                requester_hex,
                                response_bytes,
                                MSG_TYPE_DELTA_RES,
                                Priority.CONTROL,
                            )
                            RNS.log(
                                f"DELTA_RES: sent {len(response.get('metadata', []))} "
                                f"records for thread {thread_id[:12]} "
                                f"to {requester_hex[:16]}",
                                RNS.LOG_DEBUG,
                            )
                    except Exception as e:
                        RNS.log(f"DELTA_REQ handler error: {e}", RNS.LOG_WARNING)

                # Schedule on the engine's event loop.
                _schedule_on_engine_loop(
                    sync_engine,
                    _handle_delta_req(content, message.source_hash),
                    "MSG_TYPE_DELTA_REQ",
                )

            elif title == MSG_TYPE_DELTA_RES:
                # ============================================================
                # Tier 3 DELTA_RESPONSE via LXMF.
                #
                # The peer processed our DELTA_REQ and sent back records.
                # Process them through the standard delta response handler,
                # trigger payload fetches.
                # ============================================================
                RNS.log(
                    f"Received DELTA_RES via LXMF from "
                    f"{message.source_hash.hex()[:16] if message.source_hash else 'unknown'}",
                    RNS.LOG_DEBUG,
                )


                # Register peer authoritatively (§9.2).
                if peer_tracker and message.source_hash:
                    source_hex = message.source_hash.hex()
                    peer_tracker.register_from_message(
                        source_hex, identity=source_identity,
                    )

                async def _handle_delta_res(res_content, source_hash_bytes):
                    """Async handler: process incoming delta response."""
                    try:
                        from retiboard.sync.delta import process_delta_response

                        res_bytes = (
                            res_content.encode("utf-8")
                            if isinstance(res_content, str)
                            else res_content
                        )

                        # Extract board_id before processing so we can clear
                        # backoff on success.
                        try:
                            res_parsed = json.loads(res_bytes)
                            delta_board_id = res_parsed.get("board_id", "")
                        except (json.JSONDecodeError, ValueError):
                            delta_board_id = ""

                        stored = await process_delta_response(
                            res_bytes,
                            peer_tracker=peer_tracker,
                            source_hash=source_hash_bytes,
                            sync_engine=sync_engine,
                        )

                        # Record success to clear accumulated backoff.
                        if stored > 0 and sync_engine and source_hash_bytes and delta_board_id:
                            sync_engine.rate_limiter.record_success(
                                delta_board_id,
                                source_hash_bytes.hex(),
                            )
                            RNS.log(
                                f"DELTA_RES: stored {stored} records from "
                                f"{source_hash_bytes.hex()[:16]} for board {delta_board_id[:8]}",
                                RNS.LOG_DEBUG,
                            )

                        # Handle pagination: if more data exists, the next
                        # HAVE cycle will detect the thread is still stale
                        # and enqueue another DELTA_REQ automatically.

                    except Exception as e:
                        RNS.log(f"DELTA_RES handler error: {e}", RNS.LOG_WARNING)

                # Schedule on the engine's event loop.
                _schedule_on_engine_loop(
                    sync_engine,
                    _handle_delta_res(content, message.source_hash),
                    "MSG_TYPE_DELTA_RES",
                )

            elif title == MSG_TYPE_PAYLOAD_RES:
                # Tier 3 (v3.6.2): Payload response received via LXMF.
                # Decode, verify, store, and signal pending fetch waiters.
                RNS.log(
                    f"Received PAYLOAD_RESPONSE via LXMF from "
                    f"{message.source_hash.hex()[:16] if message.source_hash else 'unknown'}",
                    RNS.LOG_DEBUG,
                )
                from retiboard.sync.payload_fetch import handle_payload_response_lxmf

                # Register the peer (authoritative).
                if peer_tracker and message.source_hash:
                    source_hex = message.source_hash.hex()
                    peer_tracker.register_from_message(
                        source_hex, identity=source_identity,
                    )

                handle_payload_response_lxmf(
                    content,
                    message.source_hash,
                )

            elif title == MSG_TYPE_CHUNK_MANIFEST_REQ:
                if peer_tracker and message.source_hash:
                    source_hex = message.source_hash.hex()
                    peer_tracker.register_from_message(source_hex, identity=source_identity)
                from retiboard.sync.payload_fetch import handle_chunk_manifest_request_lxmf
                _schedule_on_engine_loop(
                    sync_engine,
                    handle_chunk_manifest_request_lxmf(content, message.source_hash, source_identity, sync_engine),
                    "MSG_TYPE_CHUNK_MANIFEST_REQ",
                )

            elif title == MSG_TYPE_CHUNK_MANIFEST_RES:
                if peer_tracker and message.source_hash:
                    source_hex = message.source_hash.hex()
                    peer_tracker.register_from_message(source_hex, identity=source_identity)
                from retiboard.sync.payload_fetch import handle_chunk_manifest_response_lxmf
                _schedule_on_engine_loop(
                    sync_engine,
                    handle_chunk_manifest_response_lxmf(content, message.source_hash),
                    "MSG_TYPE_CHUNK_MANIFEST_RES",
                )

            elif title == MSG_TYPE_CHUNK_MANIFEST_UNAV:
                if peer_tracker and message.source_hash:
                    source_hex = message.source_hash.hex()
                    peer_tracker.register_from_message(source_hex, identity=source_identity)
                from retiboard.sync.payload_fetch import handle_chunk_manifest_unavailable_lxmf
                _schedule_on_engine_loop(
                    sync_engine,
                    handle_chunk_manifest_unavailable_lxmf(content, message.source_hash),
                    "MSG_TYPE_CHUNK_MANIFEST_UNAV",
                )

            elif title == MSG_TYPE_CHUNK_REQ:
                if peer_tracker and message.source_hash:
                    source_hex = message.source_hash.hex()
                    peer_tracker.register_from_message(source_hex, identity=source_identity)
                from retiboard.sync.payload_fetch import handle_chunk_request_lxmf
                _schedule_on_engine_loop(
                    sync_engine,
                    handle_chunk_request_lxmf(content, message.source_hash, source_identity, sync_engine),
                    "MSG_TYPE_CHUNK_REQ",
                )

            elif title == MSG_TYPE_CHUNK_CANCEL:
                if peer_tracker and message.source_hash:
                    source_hex = message.source_hash.hex()
                    peer_tracker.register_from_message(source_hex, identity=source_identity)
                from retiboard.sync.payload_fetch import handle_chunk_cancel_lxmf
                _schedule_on_engine_loop(
                    sync_engine,
                    handle_chunk_cancel_lxmf(content, message.source_hash, source_identity, sync_engine),
                    "MSG_TYPE_CHUNK_CANCEL",
                )

            elif title == MSG_TYPE_CHUNK_OFFER:
                if peer_tracker and message.source_hash:
                    source_hex = message.source_hash.hex()
                    peer_tracker.register_from_message(source_hex, identity=source_identity)
                from retiboard.sync.payload_fetch import handle_chunk_offer_lxmf
                _schedule_on_engine_loop(
                    sync_engine,
                    handle_chunk_offer_lxmf(content, message.source_hash),
                    "MSG_TYPE_CHUNK_OFFER",
                )

            elif title == MSG_TYPE_BOARD_LIST_REQ:
                if not sync_engine or not message.source_hash:
                    return
                # Register peer so we have a path back
                source_hex = message.source_hash.hex()
                if peer_tracker:
                    peer_tracker.register_from_message(source_hex, identity=source_identity)
                
                # Gather subscribed boards and send response
                from retiboard.config import BOARDS_DIR
                boards = []
                if BOARDS_DIR.exists():
                    boards = [
                        d.name for d in BOARDS_DIR.iterdir()
                        if d.is_dir() and (d / "meta.db").exists()
                    ]
                
                from retiboard.sync.message_queue import Priority
                payload = json.dumps({"boards": boards}, separators=(",", ":")).encode("utf-8")
                
                # Since we already have the path from the incoming message, send directly
                sync_engine.send_lxmf(
                    source_hex,
                    payload,
                    MSG_TYPE_BOARD_LIST_RES,
                    Priority.CONTROL,
                )

            elif title == MSG_TYPE_BOARD_LIST_RES:
                if not sync_engine or not message.source_hash:
                    return
                source_hex = message.source_hash.hex()
                if peer_tracker:
                    peer_tracker.register_from_message(source_hex, identity=source_identity)
                
                try:
                    data = json.loads(content)
                    boards = data.get("boards", [])
                    if isinstance(boards, list):
                        # Pass back to BoardManager to handle discovery and cold-start push
                        bm = getattr(sync_engine, "_board_manager", None)
                        if bm:
                            bm._on_board_list_received(source_hex, boards)
                except Exception as e:
                    RNS.log(f"Error parsing board list response: {e}", RNS.LOG_DEBUG)

            elif title == MSG_TYPE_BOARD_ANNOUNCE:
                # Board announce pushed via LXMF (cold-start race fix).
                #
                # When a peer discovers us via LXMF identity announce, they
                # push their owned board announces in case we missed the
                # original RNS broadcast. We process it through the same
                # path as a regular board announce: validate, cache, and
                # add to the discovered-boards list for the user.
                #
                # The sender's LXMF hash (message.source) is authoritative
                # (§9.2) — we use it as the peer_lxmf_hash for the board.
                RNS.log(
                    f"Received board announce via LXMF from "
                    f"{message.source_hash.hex()[:16] if message.source_hash else 'unknown'}",
                    RNS.LOG_DEBUG,
                )

                # Register peer authoritatively.
                if peer_tracker and message.source_hash:
                    source_hex = message.source_hash.hex()
                    peer_tracker.register_from_message(
                        source_hex, identity=source_identity,
                    )

                try:
                    from retiboard.boards.announce import (
                        parse_announce_data,
                        validate_announce_fields,
                        get_board_id_from_announce,
                    )
                    from retiboard.db.models import Board

                    announce_bytes = content.encode("utf-8") if isinstance(content, str) else content
                    data = parse_announce_data(announce_bytes)
                    if data is None:
                        RNS.log("Board announce via LXMF: invalid JSON", RNS.LOG_DEBUG)
                    elif not validate_announce_fields(data):
                        RNS.log("Board announce via LXMF: invalid fields", RNS.LOG_DEBUG)
                    else:
                        board = Board.from_announce_dict(data)
                        board_id = get_board_id_from_announce(data)

                        # Override peer_lxmf_hash with the authoritative
                        # message.source — the sender IS the board owner.
                        if message.source_hash:
                            board.peer_lxmf_hash = message.source_hash.hex()

                        RNS.log(
                            f"Board announce via LXMF: '{board.display_name}' "
                            f"({board_id[:8]}) from {board.peer_lxmf_hash[:16]}",
                            RNS.LOG_DEBUG,
                        )

                        # Feed into the board manager's announce processing
                        # via the same callback used by RNS broadcast announces.
                        # This caches key_material, updates the board_config if
                        # already subscribed, and registers the peer.
                        if sync_engine and sync_engine._board_manager:
                            bm = sync_engine._board_manager
                            bm._on_announce_received(
                                board_id, source_identity, board,
                            )
                            # Also cache in the announce handler so the board
                            # appears in get_discovered_boards().
                            bm._announce_handler.received_announces[board_id] = board
                except Exception as e:
                    RNS.log(
                        f"Error processing board announce via LXMF: {e}",
                        RNS.LOG_WARNING,
                    )

            else:
                RNS.log(
                    f"Unknown LXMF message type: {title}",
                    RNS.LOG_DEBUG,
                )

        except Exception as e:
            RNS.log(
                f"Error in LXMF delivery callback: {e}",
                RNS.LOG_WARNING,
            )

    return delivery_callback
