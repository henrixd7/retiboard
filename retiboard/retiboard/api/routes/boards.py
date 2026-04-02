"""
Board management API routes.

Spec references:
    §3.3 — Board announce schema
    §5   — key_material passthrough to frontend only
    §9   — Board discovery

Endpoints:
    POST   /api/boards              — Create a new board + announce
    GET    /api/boards              — List subscribed boards
    GET    /api/boards/discovered   — List boards seen but not subscribed
    GET    /api/boards/{board_id}   — Get board details (including key_material)
    POST   /api/boards/{board_id}/subscribe — Subscribe to a discovered board
    DELETE /api/boards/{board_id}   — Unsubscribe + full local purge

Design:
    - key_material is returned in GET responses for the frontend to derive
      the AES-GCM board key per session (§5, §10).
    - key_material is NEVER read from the database — it comes from the
      in-memory cache or on-disk announce cache via BoardManager.
    - DELETE triggers rm -rf of the entire board directory (§4).
"""

from __future__ import annotations


from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field


# =============================================================================
# Request/Response models
# =============================================================================

class CreateBoardRequest(BaseModel):
    """Request body for creating a new board."""
    display_name: str = Field(..., min_length=1, max_length=128)
    text_only: bool = False
    default_ttl_seconds: int = Field(default=43_200, ge=10, le=2_592_000)
    bump_decay_rate: int = Field(default=3_600, ge=5, le=86_400)
    max_active_threads_local: int = Field(default=50, ge=5, le=500)
    pow_difficulty: int = Field(default=0, ge=0)


class BoardResponse(BaseModel):
    """Board info returned by API endpoints."""
    board_id: str
    display_name: str
    text_only: bool
    default_ttl_seconds: int
    bump_decay_rate: int
    max_active_threads_local: int
    pow_difficulty: int
    key_material: str           # Passed through for frontend ONLY
    announce_version: int
    subscribed_at: float


class BoardListResponse(BaseModel):
    """Response for list endpoints."""
    boards: list[BoardResponse]
    count: int


class DiscoveredBoardResponse(BoardResponse):
    """Discovered-board response with advisory structural telemetry."""

    name_key: str
    first_seen_at: float
    last_seen_at: float
    announce_seen_count: int
    advertising_peer_count: int
    verified_peer_count: int
    owner_peer_hash: str


class DiscoveredBoardListResponse(BaseModel):
    """Response for discovered-board listings."""

    boards: list[DiscoveredBoardResponse]
    count: int
    stale_after_seconds: int
    advisory_order: list[str]


# =============================================================================
# Router factory
# =============================================================================

def create_boards_router(board_manager) -> APIRouter:
    """
    Create the boards API router.

    Args:
        board_manager: The BoardManager instance (from main.py startup).

    Returns:
        Configured APIRouter with all board endpoints.
    """
    router = APIRouter(prefix="/api/boards", tags=["boards"])

    # -----------------------------------------------------------------
    # POST /api/boards — Create a new board
    # -----------------------------------------------------------------
    @router.post("", response_model=BoardResponse, status_code=201)
    async def create_board(req: CreateBoardRequest):
        """
        Create a new board and announce it on the RNS network.

        The board_id is derived from the RNS Destination hash.
        key_material is auto-generated (random 32 bytes hex).
        The announce is signed by this node's identity (§12.1).
        """
        board = await board_manager.create_board(
            display_name=req.display_name,
            text_only=req.text_only,
            default_ttl_seconds=req.default_ttl_seconds,
            bump_decay_rate=req.bump_decay_rate,
            max_active_threads_local=req.max_active_threads_local,
            pow_difficulty=req.pow_difficulty,
        )
        return _board_to_response(board)

    # -----------------------------------------------------------------
    # GET /api/boards — List subscribed boards
    # -----------------------------------------------------------------
    @router.get("", response_model=BoardListResponse)
    async def list_boards():
        """
        List all locally subscribed boards.

        key_material is included in each board's response so the
        frontend can derive board keys per session (§5, §10).
        """
        boards = await board_manager.list_boards()
        return BoardListResponse(
            boards=[_board_to_response(b) for b in boards],
            count=len(boards),
        )

    # -----------------------------------------------------------------
    # GET /api/boards/discovered — List discovered (not yet subscribed)
    # -----------------------------------------------------------------
    @router.get("/discovered", response_model=DiscoveredBoardListResponse)
    async def list_discovered():
        """
        List boards discovered via network announces but not yet subscribed.
        """
        boards = board_manager.get_discovered_boards()
        return DiscoveredBoardListResponse(
            boards=[
                _discovered_board_to_response(item)
                for item in boards
            ],
            count=len(boards),
            stale_after_seconds=board_manager.discovered_board_stale_seconds(),
            advisory_order=board_manager.discovery_order_fields(),
        )

    # -----------------------------------------------------------------
    # GET /api/boards/{board_id} — Get a single board
    # -----------------------------------------------------------------
    @router.get("/{board_id}", response_model=BoardResponse)
    async def get_board(board_id: str):
        """
        Get details for a subscribed board.

        Returns key_material for frontend key derivation (§5, §10).
        key_material comes from the in-memory cache, NOT from the database.
        """
        board = await board_manager.get_board(board_id)
        if board is None:
            raise HTTPException(status_code=404, detail="Board not found")
        return _board_to_response(board)

    # -----------------------------------------------------------------
    # POST /api/boards/{board_id}/subscribe — Subscribe to discovered
    # -----------------------------------------------------------------
    @router.post("/{board_id}/subscribe", response_model=BoardResponse)
    async def subscribe_to_board(board_id: str):
        """
        Subscribe to a board that was discovered via announce.

        The board must have been seen in a network announce first.
        """
        discovered = board_manager._announce_handler.received_announces
        board = discovered.get(board_id)
        if board is None:
            raise HTTPException(
                status_code=404,
                detail="Board not found in discovered announces. "
                       "Wait for the board to announce on the network.",
            )

        await board_manager.subscribe(board)
        # Reload from manager to get the full config.
        result = await board_manager.get_board(board_id)
        if result is None:
            raise HTTPException(status_code=500, detail="Subscribe failed")
        return _board_to_response(result)

    # -----------------------------------------------------------------
    # DELETE /api/boards/{board_id} — Unsubscribe + purge
    # -----------------------------------------------------------------
    @router.delete("/{board_id}", status_code=200)
    async def unsubscribe_board(board_id: str):
        """
        Unsubscribe from a board and purge all local data.

        Per §4: full rm -rf of the board directory (meta.db, payloads,
        announce cache). This is irreversible.
        """
        existed = await board_manager.unsubscribe(board_id)
        if not existed:
            raise HTTPException(status_code=404, detail="Board not found")
        return {"status": "ok", "board_id": board_id, "purged": True}

    # -----------------------------------------------------------------
    # POST /api/boards/{board_id}/reannounce — Re-broadcast announce
    # -----------------------------------------------------------------
    @router.post("/{board_id}/reannounce", status_code=200)
    async def reannounce_board(board_id: str):
        """
        Re-broadcast the announce for a board we own.

        Useful for refreshing network presence after connectivity changes.
        Only works for boards created by this node.
        """
        success = await board_manager.re_announce(board_id)
        if not success:
            raise HTTPException(
                status_code=400,
                detail="Cannot re-announce: board not owned by this node",
            )
        return {"status": "ok", "board_id": board_id, "reannounced": True}

    return router


def _board_to_response(board) -> BoardResponse:
    """Convert a Board model to an API response."""
    return BoardResponse(
        board_id=board.board_id,
        display_name=board.display_name,
        text_only=board.text_only,
        default_ttl_seconds=board.default_ttl_seconds,
        bump_decay_rate=board.bump_decay_rate,
        max_active_threads_local=board.max_active_threads_local,
        pow_difficulty=board.pow_difficulty,
        key_material=board.key_material,
        announce_version=board.announce_version,
        subscribed_at=board.subscribed_at,
    )


def _discovered_board_to_response(item) -> DiscoveredBoardResponse:
    """Convert a discovered-board snapshot to an API response."""
    board = item.board
    return DiscoveredBoardResponse(
        board_id=board.board_id,
        display_name=board.display_name,
        text_only=board.text_only,
        default_ttl_seconds=board.default_ttl_seconds,
        bump_decay_rate=board.bump_decay_rate,
        max_active_threads_local=board.max_active_threads_local,
        pow_difficulty=board.pow_difficulty,
        key_material=board.key_material,
        announce_version=board.announce_version,
        subscribed_at=board.subscribed_at,
        name_key=item.name_key,
        first_seen_at=item.first_seen_at,
        last_seen_at=item.last_seen_at,
        announce_seen_count=item.announce_seen_count,
        advertising_peer_count=item.advertising_peer_count,
        verified_peer_count=item.verified_peer_count,
        owner_peer_hash=board.peer_lxmf_hash,
    )
