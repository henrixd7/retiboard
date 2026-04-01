"""
Tier 3 — Delta Gossip (on-demand, priority-based).

Spec references:
    §7.1 Tier 3 — DELTA_REQUEST/DELTA_RESPONSE schemas.
                  "Responses limited to max 50 metadata records or 16 KB
                   per packet (whichever comes first)."
                  "If more records exist, more:true and the requester issues
                   another DELTA_REQUEST with an updated since_timestamp."
                  "Responder never includes posts from abandoned threads."

DELTA_REQUEST schema:
    {"board_id": str, "thread_id": str, "since_timestamp": int, "known_post_count": int}

DELTA_RESPONSE schema:
    {"board_id": str, "thread_id": str, "metadata": [...], "more": bool}

This module provides:
    - build_delta_response(): query local DB for posts newer than since_timestamp
    - handle_delta_response(): process incoming delta, validate and store each post
    - Request handler for RNS Destination.register_request_handler()
"""

from __future__ import annotations

import json
from typing import Optional, TYPE_CHECKING

import RNS

from retiboard.config import DELTA_MAX_RECORDS, DELTA_MAX_BYTES

if TYPE_CHECKING:
    from retiboard.sync.peers import PeerTracker


async def build_delta_response(
    board_id: str,
    thread_id: str,
    since_timestamp: int,
    known_post_count: int,
) -> dict:
    """
    Build a DELTA_RESPONSE for a thread.

    Queries local DB for posts in the thread with timestamp > since_timestamp,
    respecting the 50-record / 16 KB batch limits.  Each post is checked
    against the local moderation/replication policy before inclusion —
    hidden, purged, or blocked-identity posts are silently excluded so that
    the node never gossips content it has decided not to share (§7.4, §22).

    Args:
        board_id: Board ID.
        thread_id: Thread to fetch deltas for.
        since_timestamp: Only return posts newer than this.
        known_post_count: Requester's known count (for logging).

    Returns:
        DELTA_RESPONSE dict with "metadata" list and "more" flag.
    """
    # Late imports keep module-level import graph clean and match the
    # pattern used throughout the sync package.
    from retiboard.db.database import open_board_db, is_board_subscribed
    from retiboard.db.models import PostMetadata
    from retiboard.moderation.policy import should_replicate_post

    # Don't create ghost directories when building responses for
    # boards we're not subscribed to.
    if not is_board_subscribed(board_id):
        return {
            "board_id": board_id,
            "thread_id": thread_id,
            "metadata": [],
            "more": False,
        }

    db = await open_board_db(board_id)
    try:
        # Query posts newer than since_timestamp, ordered chronologically.
        # Retention is thread-scoped: active threads export all of their posts.
        async with db.execute(
            """
            SELECT * FROM posts
            WHERE thread_id = ?
              AND timestamp > ?
              AND is_abandoned = 0
            ORDER BY timestamp ASC
            """,
            (thread_id, since_timestamp),
        ) as cur:
            rows = await cur.fetchall()

        metadata_list = []
        total_bytes = 0
        more = False

        for row in rows:
            encrypted_pings = []
            if "encrypted_pings" in row.keys() and row["encrypted_pings"]:
                try:
                    parsed_pings = json.loads(row["encrypted_pings"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    parsed_pings = []
                if isinstance(parsed_pings, list):
                    encrypted_pings = [
                        item for item in parsed_pings if isinstance(item, str)
                    ]

            post = PostMetadata(
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
                public_key=row["public_key"] if "public_key" in row.keys() else "",
                encrypted_pings=encrypted_pings,
                edit_signature=row["edit_signature"] if "edit_signature" in row.keys() else "",
                thread_last_activity=row["thread_last_activity"],
                is_abandoned=bool(row["is_abandoned"]),
            )

            # Policy gate: respect per-post and per-thread moderation rules.
            # Under current policy this excludes abandoned and purged content;
            # hidden content remains shareable and is filtered only in the UI.
            decision = await should_replicate_post(db, post)
            if not decision.allowed:
                RNS.log(
                    f"Delta: skipping post {post.post_id[:12]} ({decision.reason})",
                    RNS.LOG_DEBUG,
                )
                continue

            record = {
                "post_id": row["post_id"],
                "thread_id": row["thread_id"],
                "parent_id": row["parent_id"],
                "timestamp": row["timestamp"],
                "bump_flag": bool(row["bump_flag"]),
                "content_hash": row["content_hash"],
                "payload_size": row["payload_size"],
                "attachment_content_hash": row["attachment_content_hash"],
                "attachment_payload_size": row["attachment_payload_size"],
                "has_attachments": bool(row["has_attachments"]),
                "attachment_count": row["attachment_count"] if "attachment_count" in row.keys() else 0,
                "text_only": bool(row["text_only"]),
                "identity_hash": row["identity_hash"],
                "pow_nonce": row["pow_nonce"],
                "public_key": post.public_key,
                "encrypted_pings": post.encrypted_pings,
                "edit_signature": post.edit_signature,
                "thread_last_activity": row["thread_last_activity"],
                "is_abandoned": bool(row["is_abandoned"]),
            }

            record_json = json.dumps(record, separators=(",", ":"))
            record_size = len(record_json.encode("utf-8"))

            # Check batch limits.
            if len(metadata_list) >= DELTA_MAX_RECORDS:
                more = True
                break
            if total_bytes + record_size > DELTA_MAX_BYTES:
                more = True
                break

            metadata_list.append(record)
            total_bytes += record_size

        return {
            "board_id": board_id,
            "thread_id": thread_id,
            "metadata": metadata_list,
            "more": more,
        }

    finally:
        await db.close()


def delta_request_handler(path, data, request_id, link_id, remote_identity, requested_at):
    """
    LEGACY — RNS Link request handler for DELTA_REQUEST.

    =========================================================================
    STATUS: DEPRECATED — kept for backward compatibility with v3.6.1 peers.
    Active delta sync now uses LXMF messages (MSG_TYPE_DELTA_REQ/RES) via
    the delivery callback in receiver.py.
    =========================================================================

    DEBUGGING HISTORY (do not repeat these approaches):

    Problem: RNS request handlers run synchronously in the transport thread.
    build_delta_response() requires async DB access (aiosqlite).  Every
    approach to bridge sync→async blocks the transport thread, and RNS
    tears down the link before the response can be transmitted.

    Attempt 1 — asyncio.run() directly:
      → Creates nested event loop, blocks RNS thread during DB query.
      → Result: "Attempt to transmit over a closed link, dropping packet"

    Attempt 2 — asyncio.run() in try/except RuntimeError:
      → Same blocking, same result.  The try/except just masked the
        "event loop already running" error.

    Attempt 3 — ThreadPoolExecutor + asyncio.new_event_loop():
      → DB work runs in worker thread, but future.result(timeout=10)
        still blocks the RNS handler thread.  Same link closure.

    Root cause: The RNS request handler contract requires synchronous return.
    There is NO way to return a response without blocking the thread that
    processes link maintenance.  The link always dies.

    Solution: Switched to LXMF message exchange (MSG_TYPE_DELTA_REQ/RES)
    which is fully async.  See receiver.py make_delivery_callback() for
    the handlers.  This mirrors HAVE_REQ/HAVE which works flawlessly.
    =========================================================================
    """
    import asyncio
    import concurrent.futures

    try:
        if isinstance(data, bytes):
            req = json.loads(data.decode("utf-8"))
        else:
            req = json.loads(str(data))

        board_id = req.get("board_id", "")
        thread_id = req.get("thread_id", "")
        since_timestamp = req.get("since_timestamp", 0)
        known_post_count = req.get("known_post_count", 0)

        if not board_id or not thread_id:
            RNS.log("Invalid DELTA_REQUEST: missing fields", RNS.LOG_WARNING)
            return None

        RNS.log(
            f"DELTA_REQUEST for thread {thread_id[:12]} "
            f"since {since_timestamp} from link {RNS.prettyhexrep(link_id) if link_id else 'unknown'}",
            RNS.LOG_DEBUG,
        )

        # Run the async DB query in a dedicated thread with its own
        # event loop.  This avoids blocking the RNS transport thread
        # (which needs to remain free to transmit the response).
        def _run_in_new_loop():
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(
                    build_delta_response(
                        board_id, thread_id, since_timestamp, known_post_count,
                    )
                )
            finally:
                loop.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_run_in_new_loop)
            response = future.result(timeout=10)

        result = json.dumps(response, separators=(",", ":")).encode("utf-8")

        RNS.log(
            f"DELTA_RESPONSE: {len(response.get('metadata', []))} records, "
            f"more={response.get('more', False)}",
            RNS.LOG_DEBUG,
        )

        return result

    except Exception as e:
        RNS.log(f"Error handling DELTA_REQUEST: {e}", RNS.LOG_WARNING)
        return None


async def process_delta_response(
    response_data: bytes,
    peer_tracker: Optional["PeerTracker"] = None,
    source_hash: Optional[bytes] = None,
    sync_engine=None,
) -> int:
    """
    Process an incoming DELTA_RESPONSE.

    Validates and stores each metadata record via the receiver module.

    Args:
        response_data: Raw DELTA_RESPONSE JSON bytes.
        peer_tracker: For tracking the source peer.
        source_hash: Source peer hash.
        sync_engine: SyncEngine for opportunistic replication (v3.6.3).

    Returns:
        Number of new posts stored.
    """
    from retiboard.sync.receiver import handle_incoming_metadata

    try:
        response = json.loads(response_data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        RNS.log("Invalid DELTA_RESPONSE JSON", RNS.LOG_WARNING)
        return 0

    board_id = response.get("board_id", "")
    metadata_list = response.get("metadata", [])
    more = response.get("more", False)

    # Gate: don't store data for boards we're not subscribed to.
    from retiboard.db.database import is_board_subscribed
    if not is_board_subscribed(board_id):
        RNS.log(
            f"Delta response for unsubscribed board {board_id[:8]}, ignoring",
            RNS.LOG_DEBUG,
        )
        return 0

    stored = 0
    for meta_dict in metadata_list:
        result = await handle_incoming_metadata(
            meta_dict, board_id,
            source_hash=source_hash,
            peer_tracker=peer_tracker,
            sync_engine=sync_engine,
        )
        if result:
            stored += 1

    RNS.log(
        f"Delta response: stored {stored}/{len(metadata_list)} records "
        f"for board {board_id[:8]}, more={more}",
        RNS.LOG_DEBUG,
    )

    return stored
