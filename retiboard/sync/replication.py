"""
Opportunistic replication — forward new content to nearby peers.

Spec references:
    §7.1 — "When a client receives new content for an active thread, it
           forwards the metadata (and optionally the payload) to 1-3
           recently seen peers (fan-out ≤ 3). This is the only push
           mechanism."

Design:
    When we receive and store new metadata (via Tier 1 or Tier 3), we
    opportunistically forward it to a few recently seen peers. This helps
    content propagate without requiring every peer to do a full HAVE cycle.

    Only METADATA is pushed — payloads are always on-demand (§7.1).
    Abandoned threads are never replicated.
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
from retiboard.db.database import open_board_db
from retiboard.moderation.policy import should_replicate_post

if TYPE_CHECKING:
    from retiboard.sync.peers import PeerTracker
    from retiboard.db.models import PostMetadata


async def replicate_metadata(
    router,  # LXMF.LXMRouter
    source_destination,  # Our LXMF delivery destination
    post: "PostMetadata",
    board_id: str,
    peer_tracker: "PeerTracker",
    exclude_source: Optional[bytes] = None,
    sync_engine=None,
) -> int:
    """
    Opportunistically forward post metadata to nearby peers.

    §7.1: fan-out ≤ 3, exclude the peer we received it from.
    v3.6.2 §7.1/§8: Uses send_lxmf for path-aware queued delivery.

    Args:
        router: LXMF router.
        source_destination: Our LXMF delivery destination.
        post: The PostMetadata to replicate.
        board_id: Board this post belongs to.
        peer_tracker: For finding replication targets.
        exclude_source: Destination hash of the peer we got this from
                        (to avoid echo).
        sync_engine: SyncEngine for queued delivery (v3.6.2).

    Returns:
        Number of peers the metadata was forwarded to.
    """
    if not HAS_LXMF:
        return 0

    db = await open_board_db(board_id)
    try:
        decision = await should_replicate_post(db, post)
    finally:
        await db.close()
    if not decision.allowed:
        return 0

    # Also exclude self.
    exclude_hex = exclude_source.hex() if isinstance(exclude_source, bytes) else exclude_source
    self_hash = source_destination.hexhash if source_destination else None

    targets = peer_tracker.get_replication_targets(
        board_id, exclude_hash=exclude_hex,
    )
    # Filter out self.
    if self_hash:
        targets = [p for p in targets if p.lxmf_hash != self_hash]

    if not targets:
        return 0

    meta_dict = post.to_dict()
    meta_dict["_board_id"] = board_id
    content = json.dumps(meta_dict, separators=(",", ":"))
    content_bytes = content.encode("utf-8")

    forwarded = 0
    for peer in targets:
        if peer.identity is None:
            continue

        # v3.6.2: use send_lxmf for path-aware queued delivery.
        if sync_engine is not None:
            from retiboard.sync.message_queue import Priority
            ok = sync_engine.send_lxmf(
                peer.lxmf_hash, content_bytes,
                MSG_TYPE_METADATA, Priority.DATA,
            )
            if ok:
                forwarded += 1
        else:
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
                forwarded += 1
            except Exception as e:
                RNS.log(
                    f"Replication to {peer.hexhash[:16]} failed: {e}",
                    RNS.LOG_DEBUG,
                )

    if forwarded > 0:
        RNS.log(
            f"Replicated post {post.post_id[:12]} to {forwarded} peer(s)",
            RNS.LOG_DEBUG,
        )

    return forwarded
