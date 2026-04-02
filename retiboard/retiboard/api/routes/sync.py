"""
WebSocket endpoint for real-time metadata push.

Spec references:
    §10 — "Virtualized catalog + thread views" — the frontend needs
          real-time updates to avoid polling.
    §2.2 — Served on localhost only.

Design:
    WebSocket at /ws/boards/{board_id} pushes new structural metadata
    to connected frontends as posts arrive (from local creation or gossip).

    Messages are JSON with the §3.1 metadata schema plus an "event" field.
    The frontend receives these, updates its catalog/thread views, and
    fetches+decrypts payloads on-demand.

    NEVER sends plaintext, key_material, or decrypted content.

    The WebSocket manager maintains a set of connected clients per board
    and provides a broadcast method called by the post creation path
    and the gossip receiver.
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

import RNS


class WebSocketManager:
    """
    Manages WebSocket connections per board for real-time updates.

    Thread-safe: uses asyncio primitives (safe within the event loop).
    """

    def __init__(self):
        # {board_id: set of WebSocket connections}
        self._connections: dict[str, set[WebSocket]] = {}

    async def connect(self, board_id: str, ws: WebSocket) -> None:
        """Accept and register a WebSocket connection for a board."""
        await ws.accept()
        if board_id not in self._connections:
            self._connections[board_id] = set()
        self._connections[board_id].add(ws)
        RNS.log(
            f"WebSocket connected for board {board_id[:8]} "
            f"({len(self._connections[board_id])} client(s))",
            RNS.LOG_DEBUG,
        )

    def disconnect(self, board_id: str, ws: WebSocket) -> None:
        """Remove a WebSocket connection."""
        if board_id in self._connections:
            self._connections[board_id].discard(ws)
            if not self._connections[board_id]:
                del self._connections[board_id]

    async def broadcast_to_board(
        self,
        board_id: str,
        event: str,
        data: dict,
    ) -> int:
        """
        Broadcast a JSON message to all connected clients for a board.

        Args:
            board_id: Board to broadcast to.
            event: Event type (e.g., "new_post", "thread_update").
            data: Structural metadata dict (§3.1 — ZERO content).

        Returns:
            Number of clients the message was sent to.
        """
        clients = self._connections.get(board_id, set())
        if not clients:
            return 0

        message = json.dumps({"event": event, "data": data})
        dead = set()
        sent = 0

        for ws in clients:
            try:
                await ws.send_text(message)
                sent += 1
            except Exception:
                dead.add(ws)

        # Clean up dead connections.
        for ws in dead:
            clients.discard(ws)

        return sent

    def client_count(self, board_id: Optional[str] = None) -> int:
        """Count connected WebSocket clients."""
        if board_id:
            return len(self._connections.get(board_id, set()))
        return sum(len(s) for s in self._connections.values())


class RegisterPeerRequest(BaseModel):
    """Request to manually register a peer (for E2E testing)."""
    peer_lxmf_hash: str
    public_key: str  # Hex encoded
    board_id: Optional[str] = None


# Singleton manager — created once, shared across the app.
ws_manager = WebSocketManager()


def create_sync_router(sync_engine=None) -> APIRouter:
    """
    Create the WebSocket sync router.

    Returns:
        APIRouter with the WebSocket endpoint.
    """
    from fastapi import HTTPException
    router = APIRouter(tags=["sync"])

    @router.websocket("/ws/boards/{board_id}")
    async def board_websocket(websocket: WebSocket, board_id: str):
        """
        WebSocket endpoint for real-time board updates.

        The client connects and receives JSON messages whenever new
        structural metadata arrives for this board. Messages have format:
            {"event": "new_post", "data": {§3.1 metadata dict}}

        The client can also send ping/keepalive messages; we ignore them.
        The connection stays open until the client disconnects.
        """
        await ws_manager.connect(board_id, websocket)
        try:
            # Keep the connection alive. The client may send keepalives.
            while True:
                try:
                    # Wait for client messages (pings, or just keep alive).
                    _ = await asyncio.wait_for(
                        websocket.receive_text(), timeout=60.0,
                    )
                except asyncio.TimeoutError:
                    # Send a ping to check connection is alive.
                    try:
                        await websocket.send_json({"event": "ping"})
                    except Exception:
                        break
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            ws_manager.disconnect(board_id, websocket)
            RNS.log(
                f"WebSocket disconnected for board {board_id[:8]}",
                RNS.LOG_DEBUG,
            )

    @router.post("/api/peers")
    async def register_peer(req: RegisterPeerRequest):
        """Manually register a peer (E2E utility)."""
        if sync_engine is None:
            raise HTTPException(status_code=503, detail="Sync engine not available")
        
        try:
            peer_identity = RNS.Identity.from_bytes(bytes.fromhex(req.public_key))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid public key: {e}")
            
        sync_engine.peer_tracker.register_from_message(
            req.peer_lxmf_hash,
            board_id=req.board_id,
            identity=peer_identity
        )
        return {"ok": True, "peer_lxmf_hash": req.peer_lxmf_hash}

    return router
