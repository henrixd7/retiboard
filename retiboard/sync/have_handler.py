"""
Tier 2 — HAVE announcement handler.

When we receive a HAVE announcement (via RNS announce app_data), this
module compares the remote thread state to our local state and enqueues
DELTA_REQUESTs for any threads where the remote side has newer data.

This is called from the BoardAnnounceHandler or from LXMF delivery
when a HAVE-type message arrives.
"""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING

import RNS

from retiboard.sync.have import parse_have, compare_have_to_local
from retiboard.db.database import is_board_subscribed

if TYPE_CHECKING:
    from retiboard.sync.peers import PeerTracker
    from retiboard.sync.engine import SyncEngine


# Module-level reference to the sync engine, set during startup.
_sync_engine: Optional["SyncEngine"] = None


def set_sync_engine(engine: "SyncEngine") -> None:
    """Set the global sync engine reference for HAVE handling."""
    global _sync_engine
    _sync_engine = engine


async def handle_have_announcement(
    have_data: bytes,
    source_hash: Optional[bytes] = None,
    source_identity: Optional[RNS.Identity] = None,
    peer_tracker: Optional["PeerTracker"] = None,
    is_from_board_announce: bool = False,
) -> int:
    """
    Process an incoming HAVE announcement.

    1. Parse the HAVE packet.
    2. Track the source peer.
    3. Compare to local state.
    4. Enqueue DELTA_REQUESTs for stale threads.

    Args:
        have_data: Raw HAVE JSON bytes.
        source_hash: Sender's destination hash. NOTE: when the HAVE arrives
                     via a board announce (is_from_board_announce=True), this
                     is the BOARD destination hash, NOT the peer's LXMF hash.
        source_identity: Sender's RNS identity.
        peer_tracker: Peer tracker to update.
        is_from_board_announce: If True, source_hash is a board destination
                     hash, not a peer LXMF hash. We must resolve the actual
                     peer LXMF hash from the peer tracker.

    Returns:
        Number of stale threads detected (delta requests enqueued).
    """
    have_dict = parse_have(have_data)
    if have_dict is None:
        RNS.log("Invalid HAVE data, ignoring", RNS.LOG_DEBUG)
        return 0

    # Use sync engine's peer tracker if not provided explicitly.
    if peer_tracker is None and _sync_engine is not None:
        peer_tracker = _sync_engine.peer_tracker

    board_id = have_dict.get("board_id", "")
    if not board_id:
        return 0

    # Gate: only sync (compare/delta) for boards we are subscribed to.
    # Without this, compare_have_to_local -> open_board_db would auto-create
    # the board directory, resurrecting ghost boards after unsubscribe.
    #
    # However, a HAVE for an unsubscribed board is still useful: it tells us
    # a peer has content for that board. If we don't know about this board
    # yet, we ask the peer to send us the board announce so the user can
    # discover and subscribe to it.
    if not is_board_subscribed(board_id):
        RNS.log(
            f"HAVE for unsubscribed board {board_id[:8]}, "
            f"skipping sync (requesting board announce if unknown)",
            RNS.LOG_DEBUG,
        )

        # If we don't already know about this board, request the announce.
        if _sync_engine is not None:
            handler = getattr(_sync_engine, '_board_manager', None)
            if handler:
                known = handler._announce_handler.received_announces
                if board_id not in known:
                    # Send a board announce request to this peer.
                    # The peer will push the announce via MSG_TYPE_BOARD_ANNOUNCE.
                    _request_board_announce(board_id, source_hash, source_identity)

        return 0

    # Resolve the actual peer LXMF hash for delta request targeting.
    #
    # When a HAVE arrives via board announce, source_hash is the BOARD
    # destination hash (e.g., 06eb8375...). But delta requests and payload
    # fetches MUST target the peer's LXMF delivery destination (§2.3).
    #
    # We look up existing peers for this board to find one whose identity
    # matches the announce identity. If found, use their LXMF hash.
    # If not found, we can't send a delta request (no valid target).
    peer_lxmf_hash = None
    resolved_identity = source_identity

    if is_from_board_announce and peer_tracker and source_identity:
        # The source_hash is a board destination hash. We need to find
        # the peer's actual LXMF hash. Search existing peers for this
        # board that have the same identity.
        peers = peer_tracker.get_peers(board_id)
        for p in peers:
            if p.identity and p.identity.hash == source_identity.hash:
                peer_lxmf_hash = p.lxmf_hash
                resolved_identity = p.identity
                break

        if peer_lxmf_hash is None:
            # Peer not yet known by LXMF hash. We can't target a delta
            # request at a board destination. Log and skip delta sync —
            # the peer will be discovered via their next LXMF identity
            # announce or when they send us an LXMF message directly.
            source_hex = source_hash.hex() if source_hash else "unknown"
            RNS.log(
                f"HAVE from board announce {source_hex[:16]}: peer LXMF hash "
                f"unknown, skipping delta sync (will discover via identity announce)",
                RNS.LOG_DEBUG,
            )
            # Still register with board dest hash for tracking (advisory).
            if source_hash:
                peer_tracker.register_from_announce(
                    board_id, source_hex, source_identity,
                )
    elif source_hash:
        # HAVE arrived via LXMF — source_hash IS the peer's LXMF hash.
        source_hex = source_hash.hex() if isinstance(source_hash, bytes) else source_hash
        peer_lxmf_hash = source_hex
        if peer_tracker:
            if source_identity:
                peer_tracker.register_from_announce(board_id, source_hex, source_identity)
            else:
                peer_tracker.see_peer(board_id, source_hash, source_identity)

    thread_count = len(have_dict.get("active_threads", []))
    source_display = peer_lxmf_hash[:16] if peer_lxmf_hash else (
        source_hash.hex()[:16] if source_hash else "unknown"
    )
    RNS.log(
        f"HAVE from {source_display}: "
        f"board {board_id[:8]}, {thread_count} thread(s)",
        RNS.LOG_DEBUG,
    )

    # Process Passive Peer Exchange (PEX) data (§8.2).
    pex_list = have_dict.get("pex", [])
    if pex_list and peer_tracker:
        for entry in pex_list:
            pex_h = entry.get("h")
            pex_i_hex = entry.get("i")
            if not pex_h or not pex_i_hex:
                continue

            try:
                pex_i = RNS.Identity.from_bytes(bytes.fromhex(pex_i_hex))
                # Register discovered peer advisory-only.
                # It will be fully verified when we communicate with it.
                if peer_tracker.register_from_announce(board_id, pex_h, identity=pex_i):
                    RNS.log(
                        f"PEX: discovered peer {pex_h[:16]} for board {board_id[:8]}",
                        RNS.LOG_DEBUG,
                    )
            except Exception as e:
                RNS.log(f"PEX error: failed to parse peer identity: {e}", RNS.LOG_DEBUG)

    # Compare to local state.
    stale_threads = await compare_have_to_local(have_dict, board_id)

    if stale_threads and _sync_engine and peer_lxmf_hash:
        # We have a valid LXMF target — enqueue delta requests.
        target_hash = bytes.fromhex(peer_lxmf_hash) if peer_lxmf_hash else None
        for thread_info in stale_threads:
            _sync_engine.enqueue_delta_request(
                board_id,
                thread_info["thread_id"],
                thread_info["since_timestamp"],
                thread_info["known_post_count"],
                target_hash=target_hash,
                target_identity=resolved_identity,
            )
    elif stale_threads and not peer_lxmf_hash:
        RNS.log(
            f"HAVE comparison: {len(stale_threads)} thread(s) need sync "
            f"but no LXMF target available for board {board_id[:8]}",
            RNS.LOG_DEBUG,
        )

    if stale_threads:
        RNS.log(
            f"HAVE comparison: {len(stale_threads)} thread(s) need delta sync "
            f"on board {board_id[:8]}",
            RNS.LOG_DEBUG,
        )

    return len(stale_threads)


def _request_board_announce(
    board_id: str,
    source_hash: Optional[bytes],
    source_identity: Optional[RNS.Identity],
) -> None:
    """
    Request a board announce from a peer who sent us a HAVE for an
    unknown board.

    We send a lightweight LXMF message asking them to push the board
    announce (§3.3) for this board. The receiver handles MSG_TYPE_BOARD_ANN_REQ
    by looking up the announce in their cache and sending it back via
    MSG_TYPE_BOARD_ANNOUNCE.

    If the peer is the board owner, they have the announce cached.
    If they're just a subscriber, they also have it cached (from when
    they originally subscribed).
    """
    if _sync_engine is None or source_hash is None:
        return


    # Resolve the peer's LXMF hash.
    peer_lxmf_hash = source_hash.hex() if isinstance(source_hash, bytes) else source_hash

    # Register the peer minimally so send_lxmf can find them.
    if source_identity:
        _sync_engine.peer_tracker.register_from_announce(
            board_id, peer_lxmf_hash, source_identity,
        )

    # Instead of a new message type, push our owned board announces
    # directly. But we may not own this board. A simpler approach:
    # send a HAVE_REQ for this board — the receiver will respond
    # with a HAVE, and also push the board announce if they own it.
    #
    # Actually, the simplest approach that works without new message types:
    # schedule a board announce re-request. The peer already sends board
    # announces on peer discovery. We just need to make the peer aware
    # of us so their next identity announce triggers the push.
    #
    # But we can do better: send_board_announces_to_peer works for owned
    # boards. For subscribed-but-not-owned, we push the cached announce.
    _push_known_announces_to_peer(peer_lxmf_hash, board_id)


def _push_known_announces_to_peer(peer_lxmf_hash: str, board_id: str) -> None:
    """
    Push cached board announce to a peer who doesn't know about this board.

    Works for both owned and subscribed boards — we look up the announce
    from the received_announces cache and send it via LXMF.
    """
    if _sync_engine is None:
        return

    import json
    from retiboard.sync import MSG_TYPE_BOARD_ANNOUNCE
    from retiboard.sync.message_queue import Priority
    from retiboard.boards.subscribe import load_announce_cache

    # Try to load announce from on-disk cache.
    announce_dict = load_announce_cache(board_id)
    if announce_dict is None:
        # Try in-memory received_announces.
        bm = getattr(_sync_engine, '_board_manager', None)
        if bm and hasattr(bm, '_announce_handler'):
            board = bm._announce_handler.received_announces.get(board_id)
            if board:
                announce_dict = board.to_announce_dict()

    if announce_dict is None:
        RNS.log(
            f"Cannot push announce for board {board_id[:8]}: no cached announce",
            RNS.LOG_DEBUG,
        )
        return

    payload = json.dumps(announce_dict, separators=(",", ":")).encode("utf-8")

    ok = _sync_engine.send_lxmf(
        peer_lxmf_hash,
        payload,
        MSG_TYPE_BOARD_ANNOUNCE,
        Priority.CONTROL,
    )

    if ok:
        RNS.log(
            f"Pushed board announce for {board_id[:8]} to {peer_lxmf_hash[:16]} "
            f"(triggered by HAVE for unknown board)",
            RNS.LOG_INFO,
        )
