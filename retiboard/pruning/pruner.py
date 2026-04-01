"""
Core pruning logic for RetiBoard.

Spec references:
    §4   — Pruning rules (background job every 15 min):
           - Retention is enforced at thread granularity.
           - New threads start with default_ttl_seconds.
           - Bumps refill bump_decay_rate, capped at a full default_ttl_seconds window.
           - Threads whose expiry_timestamp <= now are marked expired and then
             fully deleted.
           - Active threads are never pruned post-by-post.
           - User "hoarder mode" override allowed locally (NOT implemented in v1;
             even when added, it must NEVER prevent is_abandoned purging).
    §2.2 — "Relay-mode nodes apply exactly the same storage and pruning rules
           as regular clients."
    §3.3 — max_active_threads_local cap per board.

Design invariants:
    - Abandoned threads are FULLY DELETED: metadata rows AND payload .bin files.
      No stubs. No tombstones. No "thread was here" markers. (§4, §17)
    - Pruning is IDENTICAL for client and relay mode. (§2.2)
    - The pruner never inspects payload contents — it only deletes opaque files
      by content_hash. Content opacity is preserved.
    - Clock: we use time.time() (wall clock). The user's clock is authoritative.
      Clock skew between peers can cause premature or delayed pruning — this is
      acceptable by design. We log warnings for obviously wrong timestamps but
      never refuse to prune.
    - All DB operations use the Phase 1 async abstractions in db/database.py.
    - All payload deletions use the Phase 1 storage/payloads.py.

Pruning order (per board):
    1. Mark expired threads (expiry_timestamp <= now)
    2. Delete expired threads (metadata + payloads) — most aggressive first
    3. Enforce max_active_threads_local cap — prune oldest if over limit

    This order ensures abandoned threads are cleared before we count
    active threads for the cap check.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import RNS

from retiboard.config import BOARDS_DIR
from retiboard.settings import get_settings
from retiboard.db.database import (
    open_board_db,
    load_board_config,
    mark_expired_threads,
    delete_abandoned_threads,
    enforce_thread_cap,
    delete_chunk_manifests_for_blobs,
    get_all_active_threads_global,
    delete_threads_bulk,
)
from retiboard.storage.payloads import delete_payloads_bulk, delete_chunk_cache_bulk


@dataclass
class PruneResult:
    """
    Summary of a single prune cycle across all boards.

    Used for logging and monitoring. Never contains content.
    """
    boards_scanned: int = 0
    threads_abandoned: int = 0
    threads_deleted: int = 0       # Abandoned threads fully removed
    threads_capped: int = 0        # Threads removed by cap enforcement
    threads_quota_pruned: int = 0  # Threads removed by global quota
    payloads_deleted: int = 0      # Total .bin files removed
    errors: int = 0
    elapsed_ms: float = 0.0


async def prune_all_boards(now: Optional[int] = None) -> PruneResult:
    """
    Run a complete prune cycle across all subscribed boards.

    This is the main entry point called by the scheduler every 15 minutes.
    It scans all board directories, opens each board's DB, and applies
    the full pruning sequence.

    Args:
        now: Current unix timestamp. Injectable for testing.
             If None, uses time.time(). The user's clock is authoritative.

    Returns:
        PruneResult summarizing what was pruned.
    """
    if now is None:
        now = int(time.time())

    result = PruneResult()
    start = time.monotonic()

    # Sanity check: warn if clock seems unreasonable.
    if now < 1_000_000_000:  # Before ~2001
        RNS.log(
            f"WARNING: System clock looks wrong (now={now}). "
            "Pruning will proceed but results may be unexpected. "
            "The user's clock is authoritative — fix it if needed.",
            RNS.LOG_WARNING,
        )

    if not BOARDS_DIR.exists():
        result.elapsed_ms = (time.monotonic() - start) * 1000
        return result

    # Iterate all board directories.
    for entry in sorted(BOARDS_DIR.iterdir()):
        if not entry.is_dir():
            continue
        meta_path = entry / "meta.db"
        if not meta_path.exists():
            continue

        board_id = entry.name
        result.boards_scanned += 1

        try:
            board_result = await prune_board(board_id, now=now)

            result.threads_abandoned += board_result.threads_abandoned
            result.threads_deleted += board_result.threads_deleted
            result.threads_capped += board_result.threads_capped
            result.payloads_deleted += board_result.payloads_deleted

        except Exception as e:
            result.errors += 1
            RNS.log(
                f"Error pruning board {board_id}: {e}",
                RNS.LOG_WARNING,
            )

    # ---------------------------------------------------------------------
    # Global Quota Enforcement
    # ---------------------------------------------------------------------
    try:
        quota_result = await enforce_global_quota()
        result.threads_quota_pruned = quota_result.threads_quota_pruned
        result.payloads_deleted += quota_result.payloads_deleted
    except Exception as e:
        RNS.log(f"Error enforcing global quota: {e}", RNS.LOG_WARNING)

    result.elapsed_ms = (time.monotonic() - start) * 1000
    return result


async def enforce_global_quota() -> PruneResult:
    """
    Check total disk usage across all boards and prune oldest if over limit.
    """
    settings = get_settings()
    limit_mb = settings.get("global_storage_limit_mb", 1024)
    limit_bytes = limit_mb * 1024 * 1024

    total_bytes = 0
    # Simple estimate: sum of all payloads and chunk caches
    for board_entry in BOARDS_DIR.iterdir():
        if not board_entry.is_dir():
            continue
        payloads_dir = board_entry / "payloads"
        if payloads_dir.exists():
            total_bytes += sum(f.stat().st_size for f in payloads_dir.glob("*.bin") if f.is_file())
        chunk_cache_dir = board_entry / "chunk_cache"
        if chunk_cache_dir.exists():
            for d in chunk_cache_dir.iterdir():
                if d.is_dir():
                    total_bytes += sum(f.stat().st_size for f in d.rglob("*") if f.is_file())

    if total_bytes <= limit_bytes:
        return PruneResult()

    RNS.log(
        f"Global storage quota exceeded: {total_bytes / 1024 / 1024:.1f}MB > {limit_mb}MB. "
        "Starting emergency pruning of oldest threads.",
        RNS.LOG_ERROR,
    )

    result = PruneResult()
    # Find all active threads globally, sorted by oldest activity first.
    # Returns List[Tuple[board_id, thread_id, content_hashes, total_size, last_activity]]
    all_threads = await get_all_active_threads_global()
    
    # Prune until we are under 90% of the limit (hysteresis).
    target_bytes = int(limit_bytes * 0.9)
    bytes_to_prune = total_bytes - target_bytes

    pruned_bytes = 0
    threads_to_prune_by_board = {}

    for board_id, thread_id, hashes, size, _ in all_threads:
        if pruned_bytes >= bytes_to_prune:
            break
        
        if board_id not in threads_to_prune_by_board:
            threads_to_prune_by_board[board_id] = []
        threads_to_prune_by_board[board_id].append((thread_id, hashes))
        
        pruned_bytes += size
        result.threads_quota_pruned += 1

    # Apply deletions board-by-board.
    for board_id, thread_info in threads_to_prune_by_board.items():
        thread_ids = [t[0] for t in thread_info]
        all_hashes = []
        for _, hashes in thread_info:
            all_hashes.extend(hashes)

        db = await open_board_db(board_id)
        try:
            await delete_threads_bulk(db, thread_ids)
            await delete_chunk_manifests_for_blobs(db, all_hashes)
            delete_chunk_cache_bulk(board_id, all_hashes)
            n = delete_payloads_bulk(board_id, all_hashes)
            result.payloads_deleted += n
        finally:
            await db.close()

    if result.threads_quota_pruned > 0:
        RNS.log(
            f"Global quota emergency prune complete: removed {result.threads_quota_pruned} threads, "
            f"freed {pruned_bytes / 1024 / 1024:.1f}MB.",
            RNS.LOG_NOTICE,
        )

    return result


async def prune_board(board_id: str, now: Optional[int] = None) -> PruneResult:
    """
    Prune a single board: mark expired, delete, cap.

    Args:
        board_id: The board to prune.
        now: Current unix timestamp (injectable for testing).

    Returns:
        PruneResult for this board.
    """
    if now is None:
        now = int(time.time())

    result = PruneResult(boards_scanned=1)

    db = await open_board_db(board_id)
    try:
        # Load board config for the thread cap.
        config = await load_board_config(db)
        if config is None:
            RNS.log(
                f"Board {board_id}: no config found, skipping prune",
                RNS.LOG_WARNING,
            )
            return result

        max_threads = config.max_active_threads_local

        # -----------------------------------------------------------------
        # Step 1: Mark expired threads.
        # Threads expire when the OP row's expiry_timestamp is at or before now.
        # -----------------------------------------------------------------
        abandoned_ids = await mark_expired_threads(db, now=now)
        result.threads_abandoned = len(abandoned_ids)

        if abandoned_ids:
            RNS.log(
                f"Board {board_id}: marked {len(abandoned_ids)} thread(s) expired",
                RNS.LOG_DEBUG,
            )

        # -----------------------------------------------------------------
        # Step 2: Delete abandoned threads (metadata + payloads).
        # Per §4: "entire thread (metadata + payloads) deleted."
        # No stubs. No tombstones.
        # -----------------------------------------------------------------
        deleted_threads = await delete_abandoned_threads(db)
        result.threads_deleted = len(deleted_threads)

        # Delete payload files and ephemeral chunk state for abandoned threads.
        for thread_id, content_hashes in deleted_threads:
            await delete_chunk_manifests_for_blobs(db, content_hashes)
            delete_chunk_cache_bulk(board_id, content_hashes)
            n = delete_payloads_bulk(board_id, content_hashes)
            result.payloads_deleted += n
            if n > 0:
                RNS.log(
                    f"Board {board_id}: deleted thread {thread_id} "
                    f"({n} payload(s) + chunk state)",
                    RNS.LOG_DEBUG,
                )

        # -----------------------------------------------------------------
        # Step 3: Enforce max_active_threads_local cap.
        # If active thread count exceeds the cap, prune oldest by
        # thread_last_activity. This happens AFTER abandonment so we
        # don't count threads that were just deleted.
        # -----------------------------------------------------------------
        capped_threads = await enforce_thread_cap(db, max_threads)
        result.threads_capped = len(capped_threads)

        if capped_threads:
            for thread_id, content_hashes in capped_threads:
                await delete_chunk_manifests_for_blobs(db, content_hashes)
                delete_chunk_cache_bulk(board_id, content_hashes)
                n = delete_payloads_bulk(board_id, content_hashes)
                result.payloads_deleted += n
            RNS.log(
                f"Board {board_id}: capped {len(capped_threads)} thread(s) "
                f"(exceeded max {max_threads})",
                RNS.LOG_DEBUG,
            )

    finally:
        await db.close()

    return result
