"""
Node status and health endpoints.

Spec references:
    §2.2 — Client architecture
    §10  — Frontend state model (needs structural info for UI)

Design:
    All information is STRUCTURAL — never any content, keys, or
    decrypted data. The frontend uses this to display node health,
    peer connectivity, and board statistics.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from retiboard.config import (
    APP_NAME,
    APP_VERSION,
    SPEC_VERSION,
    PRUNE_INTERVAL_SECONDS,
)


class BoardStats(BaseModel):
    """Per-board structural statistics."""
    board_id: str
    display_name: str
    thread_count: int
    peer_count: int
    text_only: bool


class NodeStatus(BaseModel):
    """Full node status response."""
    status: str
    app: str
    version: str
    spec: str
    relay_mode: bool
    uptime_seconds: float
    boards_subscribed: int
    board_stats: list[BoardStats]
    total_peers: int
    total_peer_memberships: int
    path_summary: dict[str, int]  # §7.2 path states
    active_sync_tasks: dict       # CATCHUP / DELTA tasks
    active_fetches: list[dict]    # Current blob fetches
    sync_engine_running: bool
    lxmf_available: bool
    delta_queue_size: int
    message_queue_depth: int    # v3.6.2 §12: total queued LXMF messages
    ws_clients: int
    prune_interval: int
    # Transport awareness (§14).
    is_low_bandwidth: bool
    max_payload_size: int
    slowest_bitrate_bps: int | None


_start_time = time.time()


def create_status_router(
    board_manager,
    sync_engine=None,
    relay_mode: bool = False,
) -> APIRouter:
    """
    Create the status API router.

    Args:
        board_manager: BoardManager instance.
        sync_engine: SyncEngine instance (may be None).
        relay_mode: Whether running in relay mode.

    Returns:
        Configured APIRouter.
    """
    router = APIRouter(prefix="/api", tags=["status"])

    @router.get("/status", response_model=NodeStatus)
    async def get_status():
        """
        Comprehensive node status.

        Returns structural info about the node, boards, peers, and
        background task health. Never exposes content.
        """
        from retiboard.api.routes.sync import ws_manager
        from retiboard.transport import get_transport_info

        board_stats = []
        boards = await board_manager.list_boards()
        total_peer_memberships = 0
        board_ids = [b.board_id for b in boards]

        for b in boards:
            # Get thread count.
            try:
                from retiboard.db.database import open_board_db, get_thread_count
                db = await open_board_db(b.board_id)
                try:
                    tc = await get_thread_count(db)
                finally:
                    await db.close()
            except Exception:
                tc = 0

            pc = 0
            if sync_engine:
                pc = sync_engine.peer_tracker.peer_count(b.board_id)
            total_peer_memberships += pc

            board_stats.append(BoardStats(
                board_id=b.board_id,
                display_name=b.display_name,
                thread_count=tc,
                peer_count=pc,
                text_only=b.text_only,
            ))

        transport = get_transport_info()
        total_peers = 0
        path_summary = {"unknown": 0, "requested": 0, "known": 0, "stale": 0, "unreachable": 0}
        active_sync_tasks = {"catchup_boards": [], "delta_queue_size": 0}
        
        if sync_engine:
            total_peers = sync_engine.peer_tracker.unique_peer_count(board_ids)
            path_summary = sync_engine.peer_tracker.get_path_summary()
            active_sync_tasks = sync_engine.get_active_sync_tasks()

        from retiboard.sync.payload_fetch import get_active_chunk_sessions
        active_fetches = get_active_chunk_sessions()

        return NodeStatus(
            status="ok",
            app=APP_NAME,
            version=APP_VERSION,
            spec=SPEC_VERSION,
            relay_mode=relay_mode,
            uptime_seconds=round(time.time() - _start_time, 1),
            boards_subscribed=len(boards),
            board_stats=board_stats,
            total_peers=total_peers,
            total_peer_memberships=total_peer_memberships,
            path_summary=path_summary,
            active_sync_tasks=active_sync_tasks,
            active_fetches=active_fetches,
            sync_engine_running=sync_engine is not None and sync_engine._running,
            lxmf_available=(
                sync_engine._lxm_router is not None
                if sync_engine else False
            ),
            delta_queue_size=(
                sync_engine._delta_queue.qsize()
                if sync_engine else 0
            ),
            message_queue_depth=(
                sync_engine.message_queue.total_depth()
                if sync_engine else 0
            ),
            ws_clients=ws_manager.client_count(),
            prune_interval=PRUNE_INTERVAL_SECONDS,
            is_low_bandwidth=transport["is_low_bandwidth"],
            max_payload_size=transport["max_payload_size"],
            slowest_bitrate_bps=transport["slowest_bitrate_bps"],
        )

    @router.get("/identity")
    async def get_identity():
        """Return this node's public identity information."""
        if not board_manager or not board_manager._identity:
            raise HTTPException(status_code=500, detail="Identity not initialized")
        
        identity = board_manager._identity
        return {
            "hexhash": identity.hexhash,
            "public_key": identity.get_public_key().hex(),
            "lxmf_hash": sync_engine.get_lxmf_hash() if sync_engine else "",
        }

    return router
