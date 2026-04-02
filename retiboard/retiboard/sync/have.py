"""
Tier 2 — HAVE Announcements (lightweight thread discovery).

Spec references:
    §7.1 Tier 2 — "Every 5-15 minutes, each peer broadcasts a compact HAVE
                   packet (via LXMF or RNS announce)."
                  "Only threads where is_abandoned=false are included."
                  "HAVE announcements are capped at the 20 most recently
                   active threads."
                  "On low-bandwidth interfaces: 30-60 min, 10 threads."

HAVE schema (§7.1):
    {
        "board_id": "<hex>",
        "active_threads": [
            {"thread_id": "<hex>", "latest_post_timestamp": int, "post_count": int}
        ]
    }

Broadcasting: We use RNS Destination.announce() with the HAVE JSON as
app_data, re-using the board's destination from Phase 2. This piggybacks
on the existing announce infrastructure.

Receiving: The BoardAnnounceHandler (Phase 2) already receives all
announces. We extend it to also handle HAVE data when present. The
have_handler compares remote thread state to local state and enqueues
DELTA_REQUESTs for any threads that are newer on the remote side.
"""

from __future__ import annotations

import json
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .peers import PeerTracker


from retiboard.config import (
    HAVE_MAX_THREADS_NORMAL,
    HAVE_MAX_THREADS_LORA,
    HAVE_INTERVAL_MIN,
    HAVE_INTERVAL_MAX,
    HAVE_INTERVAL_LORA_MIN,
    HAVE_INTERVAL_LORA_MAX,
)


async def build_have_packet(
    board_id: str,
    is_low_bandwidth: bool = False,
    peer_tracker: Optional["PeerTracker"] = None,
) -> Optional[dict]:
    """
    Build a HAVE announcement for a board.

    Queries the local catalog for active threads and builds the §7.1
    Tier 2 schema. Only non-abandoned threads are included.

    Args:
        board_id: Board to build HAVE for.
        is_low_bandwidth: If True, cap at 10 threads (LoRa mode, §8.4).
        peer_tracker: If provided, include Passive Peer Exchange (PEX) (§8.2).

    Returns:
        HAVE dict ready for JSON serialization, or None if no threads.
    """
    from retiboard.db.database import open_board_db, get_catalog, get_post
    from retiboard.moderation.policy import should_replicate_post

    max_threads = (
        HAVE_MAX_THREADS_LORA if is_low_bandwidth
        else HAVE_MAX_THREADS_NORMAL
    )

    db = await open_board_db(board_id)
    try:
        catalog = await get_catalog(db, limit=max_threads)
        if not catalog:
            return None

        filtered_catalog = []
        for thread in catalog:
            op = await get_post(db, thread.op_post_id)
            if op is None:
                continue
            decision = await should_replicate_post(db, op)
            if decision.allowed:
                filtered_catalog.append(thread)

        if not filtered_catalog:
            return None

        active_threads = []
        for thread in filtered_catalog:
            active_threads.append({
                "thread_id": thread.thread_id,
                "latest_post_timestamp": thread.latest_post_timestamp,
                "post_count": thread.post_count,
            })

        packet = {
            "board_id": board_id,
            "active_threads": active_threads,
        }

        # Include PEX data if we have a tracker and aren't on LoRa (§8.2).
        if peer_tracker and not is_low_bandwidth:
            pex = peer_tracker.get_pex_peers(board_id, count=3)
            if pex:
                packet["pex"] = pex

        return packet
    finally:
        await db.close()


def serialize_have(have_dict: dict) -> bytes:
    """Serialize HAVE to compact JSON bytes for announce app_data."""
    return json.dumps(have_dict, separators=(",", ":")).encode("utf-8")


def parse_have(data: bytes) -> Optional[dict]:
    """Parse HAVE from announce app_data bytes."""
    try:
        from retiboard.config import MAX_HAVE_THREADS_IN_PACKET
        d = json.loads(data.decode("utf-8"))
        if isinstance(d, dict) and "active_threads" in d:
            # v3.6.3: Adversarial hardening — Resource Caps (§15).
            # Truncate oversized HAVE packets to prevent CPU/memory exhaustion.
            if len(d["active_threads"]) > MAX_HAVE_THREADS_IN_PACKET:
                d["active_threads"] = d["active_threads"][:MAX_HAVE_THREADS_IN_PACKET]
            return d
        return None
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def is_have_packet(data: Optional[bytes]) -> bool:
    """Quick check if announce app_data looks like a HAVE packet."""
    if data is None:
        return False
    try:
        d = json.loads(data.decode("utf-8"))
        return isinstance(d, dict) and "active_threads" in d
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False


async def compare_have_to_local(
    have_dict: dict,
    board_id: str,
) -> list[dict]:
    """
    Compare a remote HAVE to our local state.

    For each thread in the remote HAVE, check if:
    - We don't have the thread at all → need full delta
    - Our latest_post_timestamp is older → need delta since our timestamp
    - Our post_count is lower → need delta

    Returns list of threads needing sync:
        [{"thread_id": str, "since_timestamp": int, "known_post_count": int}]
    """
    from retiboard.db.database import open_board_db, get_catalog, get_post
    from retiboard.moderation.policy import should_replicate_post

    stale_threads = []

    db = await open_board_db(board_id)
    try:
        # Build local thread state lookup.
        local_catalog = await get_catalog(db, limit=100)
        local_map = {}
        for t in local_catalog:
            op = await get_post(db, t.op_post_id)
            if op is None:
                continue
            decision = await should_replicate_post(db, op)
            if not decision.allowed:
                continue
            local_map[t.thread_id] = {
                "latest_post_timestamp": t.latest_post_timestamp,
                "post_count": t.post_count,
            }

        # Compare each remote thread to local state.
        for remote_thread in have_dict.get("active_threads", []):
            tid = remote_thread.get("thread_id", "")
            if not tid:
                continue

            remote_ts = remote_thread.get("latest_post_timestamp", 0)
            remote_count = remote_thread.get("post_count", 0)

            local = local_map.get(tid)

            if local is None:
                # We don't have this thread at all.
                stale_threads.append({
                    "thread_id": tid,
                    "since_timestamp": 0,
                    "known_post_count": 0,
                })
            elif (remote_ts > local["latest_post_timestamp"]
                  or remote_count > local["post_count"]):
                # Remote has newer/more data.
                # If timestamps match but counts differ, there is an interior
                # hole (e.g. a post was purged locally then un-purged).
                # since_timestamp=latest would miss posts older than our
                # newest — use 0 to request the full thread so the peer
                # can fill any hole, not just append-only new posts.
                has_interior_hole = (
                    remote_count > local["post_count"]
                    and remote_ts <= local["latest_post_timestamp"]
                )
                stale_threads.append({
                    "thread_id": tid,
                    "since_timestamp": 0 if has_interior_hole else local["latest_post_timestamp"],
                    "known_post_count": local["post_count"],
                })

            # v3.6.3: Adversarial hardening — Resource Caps (§15).
            # Limit the number of threads we attempt to sync in one go.
            from retiboard.config import MAX_DELTA_BATCH_SIZE
            if len(stale_threads) >= MAX_DELTA_BATCH_SIZE:
                break
    finally:
        await db.close()

    return stale_threads


def get_have_interval(is_low_bandwidth: bool = False) -> tuple[int, int]:
    """
    Return the (min, max) HAVE broadcast interval in seconds.

    §7.1: "5-15 minutes (adaptive to link speed)"
    §8.4: "30-60 min on slow links"
    """
    if is_low_bandwidth:
        return (HAVE_INTERVAL_LORA_MIN, HAVE_INTERVAL_LORA_MAX)
    return (HAVE_INTERVAL_MIN, HAVE_INTERVAL_MAX)
