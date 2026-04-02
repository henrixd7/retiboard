"""
Tier 1 — LXMF Broadcast of new post metadata.

Spec references:
    §7.1 Tier 1 — "New metadata is sent as an LXMF message to the board's
                   destination. LXMF propagation nodes provide short-term
                   offline buffering."
    v3.6.2 §7.1 — Send logic: check path state, queue if no path, request.
    v3.6.2 §8   — Messages queued per-peer when path is not KNOWN.

When a new post is created locally, this module broadcasts the structural
metadata (§3.1) as an LXMF message to all known peers for that board.

Design:
    - Delegates delivery to SyncEngine.send_lxmf() which implements
      the v3.6.2 §7.1 send logic (path check → queue → request).
    - Payloads are NEVER broadcast — only metadata (§7.1).
"""

from __future__ import annotations

import json
from typing import Optional, TYPE_CHECKING

import RNS

try:
    import LXMF
    HAS_LXMF = True
except ImportError:
    HAS_LXMF = False

from retiboard.sync import MSG_TYPE_METADATA

if TYPE_CHECKING:
    from retiboard.sync.peers import PeerTracker
    from retiboard.sync.engine import SyncEngine
    from retiboard.db.models import PostMetadata


def broadcast_metadata(
    router,  # LXMF.LXMRouter
    source_destination,  # Our LXMF delivery destination
    post: "PostMetadata",
    board_id: str,
    peer_tracker: "PeerTracker",
    sync_engine: Optional["SyncEngine"] = None,
) -> int:
    """
    Broadcast post metadata to known peers via LXMF.

    Uses SyncEngine.send_lxmf() when available (v3.6.2 §7.1 + §8
    path-aware queued delivery). Falls back to direct handle_outbound.

    Args:
        router: The LXMF router instance.
        source_destination: Our registered LXMF delivery destination.
        post: The PostMetadata to broadcast.
        board_id: Board this post belongs to.
        peer_tracker: Peer tracker for finding recipients.
        sync_engine: SyncEngine for queued delivery (v3.6.2).

    Returns:
        Number of peers the message was dispatched or queued to.
    """
    if not HAS_LXMF:
        RNS.log("LXMF not available, skipping broadcast", RNS.LOG_WARNING)
        return 0

    peers = peer_tracker.get_lxmf_peers(board_id)

    # Filter out self — we already have the post locally.
    if source_destination:
        self_hash = source_destination.hexhash
        peers = [p for p in peers if p.lxmf_hash != self_hash]

    if not peers:
        RNS.log(
            f"No known peers for board {board_id[:8]}, skipping broadcast",
            RNS.LOG_DEBUG,
        )
        return 0

    # Serialize metadata to JSON (§3.1 structural only, zero content).
    meta_dict = post.to_dict()
    meta_dict["_board_id"] = board_id
    content = json.dumps(meta_dict, separators=(",", ":"))
    content_bytes = content.encode("utf-8")

    dispatched = 0
    for peer in peers:
        if peer.identity is None:
            RNS.log(
                f"Skipping peer {peer.hexhash[:16]}: no identity",
                RNS.LOG_DEBUG,
            )
            continue

        # v3.6.2 §7.1: Use send_lxmf (path-aware, queued) when available.
        if sync_engine is not None:
            from retiboard.sync.message_queue import Priority
            ok = sync_engine.send_lxmf(
                peer.lxmf_hash,
                content_bytes,
                MSG_TYPE_METADATA,
                Priority.DATA,
            )
            if ok:
                dispatched += 1
        else:
            # Fallback: fire-and-forget (pre-v3.6.2 compat).
            try:
                dest = RNS.Destination(
                    peer.identity,
                    RNS.Destination.OUT,
                    RNS.Destination.SINGLE,
                    "lxmf", "delivery",
                )
                method = LXMF.LXMessage.OPPORTUNISTIC
                if len(content) > 300:
                    method = LXMF.LXMessage.DIRECT
                lxm = LXMF.LXMessage(
                    dest, source_destination, content,
                    MSG_TYPE_METADATA, desired_method=method,
                )
                router.handle_outbound(lxm)
                dispatched += 1
            except Exception as e:
                RNS.log(
                    f"Failed to broadcast to peer {peer.hexhash[:16]}: {e}",
                    RNS.LOG_WARNING,
                )

    RNS.log(
        f"Broadcast metadata for post {post.post_id[:12]} to "
        f"{dispatched}/{len(peers)} peers",
        RNS.LOG_DEBUG,
    )
    return dispatched
