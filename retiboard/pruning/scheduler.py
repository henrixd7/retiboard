"""
Pruning background scheduler.

Spec references:
    §4   — "background job every 15 min"
    §2.2 — "Relay-mode nodes apply exactly the same storage and pruning rules
           as regular clients [...] same background prune job every 15 minutes."

Provides two integration modes:
    1. FastAPI lifespan (client mode): the pruner runs as an asyncio background
       task started during app startup and cancelled on shutdown.
    2. Standalone asyncio loop (relay mode): the pruner runs in an asyncio
       event loop with no HTTP server.

Both modes use the SAME pruning logic (prune_all_boards) — the only difference
is how the event loop is managed. This ensures §2.2 compliance.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Optional

import RNS

from retiboard.config import PRUNE_INTERVAL_SECONDS
from retiboard.pruning.pruner import prune_all_boards, PruneResult


# Module-level reference to the background task so it can be cancelled.
_prune_task: Optional[asyncio.Task] = None


async def _prune_loop(interval: int = PRUNE_INTERVAL_SECONDS) -> None:
    """
    Infinite loop that runs prune_all_boards() every `interval` seconds.

    This is the core loop used by both client and relay modes.

    The first prune runs after one full interval (not immediately on startup)
    to avoid slowing down boot. If you want an immediate prune on startup,
    call prune_all_boards() directly before starting this loop.

    Args:
        interval: Seconds between prune cycles (default: 900 = 15 min).
    """
    RNS.log(
        f"Prune scheduler started (interval: {interval}s)",
        RNS.LOG_INFO,
    )

    while True:
        try:
            await asyncio.sleep(interval)

            RNS.log("Prune cycle starting...", RNS.LOG_DEBUG)
            result = await prune_all_boards()
            _log_result(result)

        except asyncio.CancelledError:
            RNS.log("Prune scheduler cancelled", RNS.LOG_INFO)
            break
        except Exception as e:
            # Never let an exception kill the pruner loop.
            # Log and continue — the next cycle will try again.
            RNS.log(
                f"Prune cycle error (will retry next cycle): {e}",
                RNS.LOG_WARNING,
            )


def _log_result(result: PruneResult) -> None:
    """Log a prune cycle summary."""
    if (result.threads_deleted == 0
            and result.threads_capped == 0):
        # Nothing happened — only log at debug level.
        RNS.log(
            f"Prune cycle: {result.boards_scanned} board(s), "
            f"nothing to prune ({result.elapsed_ms:.0f}ms)",
            RNS.LOG_DEBUG,
        )
    else:
        RNS.log(
            f"Prune cycle complete: "
            f"{result.boards_scanned} board(s), "
            f"{result.threads_abandoned} expired, "
            f"{result.threads_deleted} thread(s) deleted, "
            f"{result.threads_capped} thread(s) capped, "
            f"{result.payloads_deleted} payload(s) removed, "
            f"{result.errors} error(s), "
            f"{result.elapsed_ms:.0f}ms",
            RNS.LOG_INFO,
        )


# =============================================================================
# FastAPI lifespan integration (client mode)
# =============================================================================

@asynccontextmanager
async def pruning_lifespan(app):
    """
    FastAPI lifespan context manager that starts/stops the prune scheduler.

    Usage in create_app():
        app = FastAPI(lifespan=pruning_lifespan)

    The pruner starts as a background asyncio task when the app starts,
    and is cleanly cancelled when the app shuts down.
    """
    global _prune_task

    # Start the background prune loop.
    _prune_task = asyncio.create_task(_prune_loop())
    RNS.log("Prune background task started (FastAPI lifespan)", RNS.LOG_INFO)

    yield  # App is running; pruner is active in background.

    # Shutdown: cancel the prune task.
    if _prune_task is not None:
        _prune_task.cancel()
        try:
            await _prune_task
        except asyncio.CancelledError:
            pass
        _prune_task = None
    RNS.log("Prune background task stopped", RNS.LOG_INFO)


# =============================================================================
# Standalone asyncio integration (relay mode)
# =============================================================================

async def run_prune_loop_standalone(
    interval: int = PRUNE_INTERVAL_SECONDS,
) -> None:
    """
    Run the prune loop as a standalone coroutine.

    Used in relay mode where there's no FastAPI app to host background tasks.
    Call this from an asyncio.run() or event loop.

    This function runs indefinitely until cancelled or interrupted.
    """
    await _prune_loop(interval=interval)
