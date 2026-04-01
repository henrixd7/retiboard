"""
High-level board lifecycle manager.

Spec references:
    §3.3 — Board announce schema
    §4   — Disk layout, board deletion = rm -rf
    §5   — key_material passthrough (never in DB)
    §9   — Board discovery
    §12.1 — Board creator identity

Provides the application-level API for:
    - Creating a new board (generates identity, destination, announces)
    - Subscribing to a discovered board
    - Listing subscribed boards
    - Unsubscribing (full local purge per §4)
    - Retrieving key_material for frontend passthrough
"""

from __future__ import annotations

import secrets
import shutil
import time
from dataclasses import dataclass, field, replace
from typing import Optional

import RNS

from retiboard.config import BOARDS_DIR
from retiboard.db.models import Board
from retiboard.db.database import (
    open_board_db,
    save_board_config,
    load_board_config,
    board_dir,
)
from retiboard.boards.announce import (
    create_board_destination,
    broadcast_announce,
    BoardAnnounceHandler,
)
from retiboard.boards.subscribe import (
    board_for_db_storage,
    save_announce_cache,
    load_announce_cache,
    recover_key_material,
)


DISCOVERED_BOARD_STALE_SECONDS = 12 * 3600
DISCOVERED_PEER_ACTIVITY_SECONDS = 3600
DISCOVERY_ORDER_FIELDS = [
    "verified_peer_count",
    "advertising_peer_count",
    "announce_seen_count",
    "last_seen_at",
]


@dataclass
class DiscoveredBoardRecord:
    """In-memory structural telemetry for a discovered board."""

    board: Board
    first_seen_at: float
    last_seen_at: float
    announce_seen_count: int = 0
    advertising_peers: dict[str, float] = field(default_factory=dict)

    def note_seen(self, now: float, count_announce: bool = True) -> None:
        if self.first_seen_at <= 0:
            self.first_seen_at = now
        self.last_seen_at = now
        if count_announce:
            self.announce_seen_count += 1

    def note_peer(self, peer_lxmf_hash: str, now: float) -> None:
        if peer_lxmf_hash:
            self.advertising_peers[peer_lxmf_hash] = now


@dataclass(frozen=True)
class DiscoveredBoardSnapshot:
    """Structural snapshot returned by the discovered-boards API."""

    board: Board
    name_key: str
    first_seen_at: float
    last_seen_at: float
    announce_seen_count: int
    advertising_peer_count: int
    verified_peer_count: int


class BoardManager:
    """
    Manages the lifecycle of all boards on this node.

    Holds references to:
    - The node's RNS identity (for creating boards we own)
    - The announce handler (for receiving board discovers)
    - In-memory cache of key_material per board
    - RNS Destinations for boards we've created (for re-announcing)

    This is a singleton-ish object created at startup and shared
    across the application.
    """

    def __init__(self, identity: RNS.Identity):
        """
        Args:
            identity: This node's persistent RNS identity (§12.1).
        """
        self._identity = identity

        # In-memory key_material cache: {board_id: key_material_string}
        # This is the ONLY place key_material lives at runtime (besides
        # the opaque announce.json cache file on disk).
        self._key_material_cache: dict[str, str] = {}
        self._discovered_boards: dict[str, DiscoveredBoardRecord] = {}

        # RNS Destinations for boards we created (for re-announcing).
        # {board_id: RNS.Destination}
        self._owned_destinations: dict[str, RNS.Destination] = {}

        # Reference to sync engine for peer tracking.
        # Set after construction via set_sync_engine().
        self._sync_engine = None

        # Announce handler for receiving board discovers from the network.
        self._announce_handler = BoardAnnounceHandler(
            on_announce=self._on_announce_received,
            on_identity_announce=self._on_identity_announce_received,
        )

        # Register the handler with RNS Transport.
        RNS.Transport.register_announce_handler(self._announce_handler)
        RNS.log("Board announce handler registered", RNS.LOG_INFO)

    def set_sync_engine(self, sync_engine) -> None:
        """
        Attach the sync engine for peer tracking.
        Called from main.py after both BoardManager and SyncEngine are created.
        """
        self._sync_engine = sync_engine

    @staticmethod
    def _normalize_board_name(display_name: str) -> str:
        """Normalize display names so collisions group predictably."""
        return " ".join((display_name or "").casefold().split())

    @staticmethod
    def discovered_board_stale_seconds() -> int:
        """Expose the stale cutoff for UI/API transparency."""
        return DISCOVERED_BOARD_STALE_SECONDS

    @staticmethod
    def discovery_order_fields() -> list[str]:
        """Expose the advisory ordering fields used for discovered boards."""
        return list(DISCOVERY_ORDER_FIELDS)

    def _subscribed_board_ids(self) -> set[str]:
        """Return locally subscribed board IDs from the on-disk registry."""
        if not BOARDS_DIR.exists():
            return set()
        return {
            d.name for d in BOARDS_DIR.iterdir()
            if d.is_dir() and (d / "meta.db").exists()
        }

    def _record_discovered_board(
        self,
        board: Board,
        *,
        now: Optional[float] = None,
        count_announce: bool = True,
    ) -> None:
        """Update the local discovered-board telemetry from a board announce."""
        now = time.time() if now is None else now
        record = self._discovered_boards.get(board.board_id)
        if record is None:
            record = DiscoveredBoardRecord(
                board=board,
                first_seen_at=now,
                last_seen_at=now,
            )
            self._discovered_boards[board.board_id] = record
        else:
            record.board = board
        record.note_seen(now, count_announce=count_announce)
        if board.peer_lxmf_hash:
            record.note_peer(board.peer_lxmf_hash, now)
        self._announce_handler.received_announces[board.board_id] = board
        self._prune_discovered_records(now=now)

    def _record_discovered_peer_advertisement(
        self,
        board_id: str,
        peer_lxmf_hash: str,
        *,
        now: Optional[float] = None,
    ) -> None:
        """Track that a peer identity announce advertised participation in a board."""
        record = self._discovered_boards.get(board_id)
        if record is None:
            board = self._announce_handler.received_announces.get(board_id)
            if board is None:
                return
            seen_at = time.time() if now is None else now
            record = DiscoveredBoardRecord(
                board=board,
                first_seen_at=seen_at,
                last_seen_at=seen_at,
            )
            self._discovered_boards[board_id] = record
        seen_at = time.time() if now is None else now
        record.note_peer(peer_lxmf_hash, seen_at)
        record.last_seen_at = max(record.last_seen_at, seen_at)
        self._prune_discovered_records(now=seen_at)

    def _prune_discovered_records(self, now: Optional[float] = None) -> None:
        """Drop stale discovered boards and stale advisory peer sightings."""
        now = time.time() if now is None else now
        subscribed_ids = self._subscribed_board_ids()
        stale_board_ids = []
        for board_id, record in self._discovered_boards.items():
            stale_peers = [
                peer_hash
                for peer_hash, seen_at in record.advertising_peers.items()
                if (now - seen_at) > DISCOVERED_PEER_ACTIVITY_SECONDS
            ]
            for peer_hash in stale_peers:
                del record.advertising_peers[peer_hash]
            if board_id in subscribed_ids:
                continue
            if (now - record.last_seen_at) > DISCOVERED_BOARD_STALE_SECONDS:
                stale_board_ids.append(board_id)

        for board_id in stale_board_ids:
            self._discovered_boards.pop(board_id, None)
            self._announce_handler.received_announces.pop(board_id, None)

    def _build_discovered_snapshot(
        self,
        record: DiscoveredBoardRecord,
        *,
        now: Optional[float] = None,
    ) -> DiscoveredBoardSnapshot:
        """Materialize a discovered-board record into API-facing fields."""
        now = time.time() if now is None else now
        active_peers = {
            peer_hash
            for peer_hash, seen_at in record.advertising_peers.items()
            if (now - seen_at) <= DISCOVERED_PEER_ACTIVITY_SECONDS
        }
        verified_peer_count = 0
        peer_tracker = getattr(self._sync_engine, "peer_tracker", None)
        if peer_tracker is not None:
            for peer_hash in active_peers:
                peer = peer_tracker.get_peer(peer_hash)
                if peer and not peer.is_expired(now) and peer.verified:
                    verified_peer_count += 1
        return DiscoveredBoardSnapshot(
            board=record.board,
            name_key=self._normalize_board_name(record.board.display_name),
            first_seen_at=record.first_seen_at,
            last_seen_at=record.last_seen_at,
            announce_seen_count=record.announce_seen_count,
            advertising_peer_count=len(active_peers),
            verified_peer_count=verified_peer_count,
        )

    # =========================================================================
    # Board creation (we are the creator/owner)
    # =========================================================================

    async def create_board(
        self,
        display_name: str,
        text_only: bool = False,
        default_ttl_seconds: int = 43_200,
        bump_decay_rate: int = 3_600,
        max_active_threads_local: int = 50,
        pow_difficulty: int = 0,
    ) -> Board:
        """
        Create a new board, announce it on the network, and subscribe locally.

        Steps:
            1. Generate key_material (random 32 bytes hex — §5)
            2. Create an RNS Destination → board_id = destination.hexhash
            3. Build the Board model
            4. Broadcast the announce (signed by our identity, §12.1)
            5. Subscribe locally (store config in DB, cache key_material)

        Args:
            display_name: Human-readable board name.
            text_only: If True, attachment payloads forbidden (§8.2).
            default_ttl_seconds: Starting thread TTL in seconds (§3.3).
            bump_decay_rate: Per-bump thread TTL refill in seconds (§3.3).
            max_active_threads_local: Local thread cap (§3.3).
            pow_difficulty: PoW difficulty (§11); 0 = disabled.

        Returns:
            The created Board model (with key_material populated).
        """
        # 1. Generate key_material: random 32 bytes as hex string.
        # Per §5: "key_material field in the signed board announce is
        # deliberately public." We generate it here; it goes into the
        # announce and is distributed to anyone who receives it.
        key_material = secrets.token_hex(32)

        # 2. Create RNS Destination for this board.
        destination = create_board_destination(self._identity)
        board_id = destination.hexhash

        # 3. Get our LXMF delivery hash for v3.6.2 announce schema (§4).
        peer_lxmf_hash = ""
        if self._sync_engine and self._sync_engine._lxmf_destination:
            peer_lxmf_hash = self._sync_engine._lxmf_destination.hexhash
        elif self._sync_engine and hasattr(self._sync_engine, 'get_lxmf_hash'):
            peer_lxmf_hash = self._sync_engine.get_lxmf_hash()

        # 4. Build the Board model.
        board = Board(
            board_id=board_id,
            display_name=display_name,
            text_only=text_only,
            default_ttl_seconds=default_ttl_seconds,
            bump_decay_rate=bump_decay_rate,
            max_active_threads_local=max_active_threads_local,
            pow_difficulty=pow_difficulty,
            key_material=key_material,
            announce_version=2,  # v3.6.2
            peer_lxmf_hash=peer_lxmf_hash,
            subscribed_at=time.time(),
        )

        # 5. Broadcast the announce on the network.
        announced = broadcast_announce(destination, board)

        # 6. Subscribe locally (board works locally even if announce fails).
        await self._subscribe_board(board)

        if self._sync_engine:
            self._sync_engine.request_identity_reannounce(force=True)

        # Track the destination so we can re-announce later.
        self._owned_destinations[board_id] = destination

        if announced:
            RNS.log(
                f"Created and announced board '{display_name}' ({board_id})",
                RNS.LOG_INFO,
            )
        else:
            RNS.log(
                f"Created board '{display_name}' ({board_id}) but "
                f"announce failed (MTU exceeded). Board works locally.",
                RNS.LOG_WARNING,
            )

        return board

    # =========================================================================
    # Subscription management
    # =========================================================================

    async def subscribe(self, board: Board) -> None:
        """
        Subscribe to a board discovered via announce.

        This stores the board config in the local DB (without key_material)
        and caches key_material in memory and on disk.

        After subscription, triggers an immediate catch-up sync to
        recover any threads we missed (§7.1 Tier 2). Without this,
        the new subscriber must wait for the next periodic HAVE cycle
        (5-15 min normal, 30-60 min LoRa) before seeing any content.
        """
        await self._subscribe_board(board)

        if self._sync_engine:
            self._sync_engine.request_identity_reannounce(force=True)

        RNS.log(
            f"Subscribed to board '{board.display_name}' ({board.board_id})",
            RNS.LOG_INFO,
        )

        # Trigger immediate catch-up sync for this board.
        # This sends HAVE_REQ to known peers so they respond with their
        # current HAVE immediately, rather than waiting for the next cycle.
        if self._sync_engine:
            try:
                await self._sync_engine.sync_board_catchup(board.board_id)
            except Exception as e:
                # Catch-up failure is non-fatal — periodic HAVE will
                # eventually sync. Log and continue.
                RNS.log(
                    f"Catch-up sync for board {board.board_id[:8]} failed: {e}",
                    RNS.LOG_DEBUG,
                )

    async def unsubscribe(self, board_id: str) -> bool:
        """
        Unsubscribe from a board: full local purge.

        Per §4: "Delete board = rm -rf the board directory."
        This removes:
            - meta.db (all posts, board config)
            - /payloads/ (all encrypted blobs)
            - announce.json (cached announce)
            - The entire board directory

        Also clears in-memory caches.

        The board's announce data is preserved in received_announces so it
        appears in get_discovered_boards() and the user can re-subscribe
        without waiting for the next network announce.

        Returns True if the board existed and was deleted.
        """
        bdir = board_dir(board_id)
        existed = bdir.exists()

        # Before purging, ensure the board's announce data is saved in
        # received_announces so it survives as a discovered board.
        # For OWNED boards, this data is typically not in received_announces
        # (we don't receive our own announces). Load from on-disk cache
        # before deleting the directory.
        if existed and board_id not in self._announce_handler.received_announces:
            announce_dict = load_announce_cache(board_id)
            if announce_dict:
                try:
                    board_obj = Board.from_announce_dict(announce_dict)
                    self._record_discovered_board(
                        board_obj,
                        now=time.time(),
                        count_announce=False,
                    )
                    RNS.log(
                        f"Preserved announce for board {board_id[:8]} in discovered cache",
                        RNS.LOG_DEBUG,
                    )
                except Exception as e:
                    RNS.log(
                        f"Could not preserve announce for {board_id[:8]}: {e}",
                        RNS.LOG_DEBUG,
                    )

        if existed:
            shutil.rmtree(str(bdir))
            RNS.log(
                f"Purged board directory: {bdir}",
                RNS.LOG_INFO,
            )

        # Clear in-memory caches.
        self._key_material_cache.pop(board_id, None)
        self._owned_destinations.pop(board_id, None)
        # NOTE: We intentionally do NOT remove the board from
        # _announce_handler.received_announces here.  Keeping it
        # ensures the board shows up in get_discovered_boards()
        # so the user can re-subscribe immediately.

        if existed:
            if self._sync_engine:
                self._sync_engine.request_identity_reannounce(force=True)
            RNS.log(
                f"Unsubscribed from board {board_id}",
                RNS.LOG_INFO,
            )
        return existed

    # =========================================================================
    # Listing and retrieval
    # =========================================================================

    async def list_boards(self) -> list[Board]:
        """
        List all locally subscribed boards.

        Scans the boards directory for board subdirectories with meta.db,
        loads each board's config, and attaches key_material from cache.

        Returns:
            List of Board objects (with key_material populated from cache).
        """
        boards = []
        boards_root = BOARDS_DIR

        if not boards_root.exists():
            return boards

        for entry in sorted(boards_root.iterdir()):
            if not entry.is_dir():
                continue
            meta_path = entry / "meta.db"
            if not meta_path.exists():
                continue

            board_id = entry.name
            try:
                db = await open_board_db(board_id)
                try:
                    config = await load_board_config(db)
                    if config is not None:
                        # Attach key_material from cache (not from DB).
                        config = replace(
                            config,
                            key_material=self.get_key_material(board_id),
                        )
                        boards.append(config)
                finally:
                    await db.close()
            except Exception as e:
                RNS.log(
                    f"Error loading board {board_id}: {e}",
                    RNS.LOG_WARNING,
                )

        return boards

    async def get_board(self, board_id: str) -> Optional[Board]:
        """
        Get a single board's config with key_material attached.

        Returns None if the board is not subscribed.
        """
        bdir = board_dir(board_id)
        if not bdir.exists() or not (bdir / "meta.db").exists():
            return None

        db = await open_board_db(board_id)
        try:
            config = await load_board_config(db)
            if config is None:
                return None
            # Attach key_material from cache.
            return replace(
                config,
                key_material=self.get_key_material(board_id),
            )
        finally:
            await db.close()

    def get_key_material(self, board_id: str) -> str:
        """
        Retrieve key_material for a board from the in-memory cache.

        Falls back to the on-disk announce cache if not in memory.
        Returns empty string if unavailable (board needs re-announce).
        """
        # Check in-memory cache first.
        km = self._key_material_cache.get(board_id)
        if km:
            return km

        # Fall back to on-disk announce cache.
        km = recover_key_material(board_id)
        if km:
            self._key_material_cache[board_id] = km
        return km or ""

    def get_discovered_boards(self) -> list[DiscoveredBoardSnapshot]:
        """
        List boards discovered via announce but not yet subscribed.

        These are boards seen on the network that we haven't subscribed to.
        Useful for a "discover boards" UI.
        """
        now = time.time()
        self._prune_discovered_records(now=now)
        subscribed_ids = self._subscribed_board_ids()
        snapshots = [
            self._build_discovered_snapshot(record, now=now)
            for board_id, record in self._discovered_boards.items()
            if board_id not in subscribed_ids
        ]
        snapshots.sort(
            key=lambda item: (
                item.verified_peer_count,
                item.advertising_peer_count,
                item.announce_seen_count,
                item.last_seen_at,
                item.board.display_name.casefold(),
                item.board.board_id,
            ),
            reverse=True,
        )
        return snapshots

    # =========================================================================
    # Re-announce (for boards we own)
    # =========================================================================

    async def re_announce(self, board_id: str) -> bool:
        """
        Re-broadcast the announce for a board we own.

        Returns True if the board was re-announced, False if we don't own it.
        """
        destination = self._owned_destinations.get(board_id)
        if destination is None:
            return False

        board = await self.get_board(board_id)
        if board is None:
            return False

        result = broadcast_announce(destination, board)
        if result:
            RNS.log(f"Re-announced board {board_id}", RNS.LOG_INFO)
        return result

    # =========================================================================
    # Startup recovery
    # =========================================================================

    async def recover_boards_on_startup(self) -> int:
        """
        Recover key_material for all subscribed boards from disk cache.

        Called during startup to populate the in-memory key_material cache
        from the on-disk announce.json files.

        Returns count of boards recovered.
        """
        count = 0
        if not BOARDS_DIR.exists():
            return count

        for entry in BOARDS_DIR.iterdir():
            if not entry.is_dir():
                continue
            board_id = entry.name
            km = recover_key_material(board_id)
            if km:
                self._key_material_cache[board_id] = km
                count += 1
                RNS.log(
                    f"Recovered key_material for board {board_id}",
                    RNS.LOG_DEBUG,
                )

        RNS.log(
            f"Recovered {count} board(s) from announce cache",
            RNS.LOG_INFO,
        )
        if count and self._sync_engine:
            self._sync_engine.request_identity_reannounce(force=True)
        return count

    # =========================================================================
    # Internal helpers
    # =========================================================================

    async def _subscribe_board(self, board: Board) -> None:
        """
        Internal: store board config in DB and cache key_material.

        1. Cache key_material in memory.
        2. Save announce data to disk (opaque announce.json).
        3. Store board config in SQLite WITHOUT key_material.
        """
        board_id = board.board_id

        # 1. Cache key_material in memory.
        self._key_material_cache[board_id] = board.key_material
        self._record_discovered_board(board, count_announce=False)

        # 2. Save full announce (including key_material) to opaque file.
        save_announce_cache(board_id, board.to_announce_dict())

        # 3. Store in DB with key_material stripped.
        db_board = board_for_db_storage(board)
        db = await open_board_db(board_id)
        try:
            await save_board_config(db, db_board)
        finally:
            await db.close()

    def _on_announce_received(
        self,
        board_id: str,
        announced_identity: RNS.Identity,
        board: Board,
    ) -> None:
        """
        Callback invoked when the announce handler receives a valid board announce.

        v3.6.2: extracts peer_lxmf_hash from the announce and registers
        the peer with the sync engine's tracker for LXMF delivery routing.

        BUG FIX (pruner "no config found"):
        If this board is already subscribed (meta.db exists), we refresh its
        board_config row in SQLite.  This handles two cases:

          1. The board was subscribed via the API *before* the announce arrived
             (race on first subscription) — the DB row may be missing or stale.
          2. The board operator updated TTL, decay rate, or pow_difficulty in a
             new announce — the pruner must see the latest values.

        save_board_config uses INSERT OR REPLACE so re-entrancy is safe.
        We schedule the async DB write via the event loop because this
        callback runs in an RNS transport thread.
        """
        self._key_material_cache[board_id] = board.key_material
        self._record_discovered_board(board)

        # Refresh board_config in SQLite if we're subscribed to this board.
        # This is the fix for "Board <id>: no config found, skipping prune".
        bdir = board_dir(board_id)
        if (bdir / "meta.db").exists():
            # We are subscribed — refresh the config row.  _subscribe_board is
            # async, so schedule it on the engine loop from this sync callback.
            if self._sync_engine and self._sync_engine._loop:
                import asyncio

                async def _refresh_config():
                    try:
                        await self._subscribe_board(board)
                        RNS.log(
                            f"Refreshed board_config for {board_id[:8]} "
                            f"from announce",
                            RNS.LOG_DEBUG,
                        )
                    except Exception as exc:
                        RNS.log(
                            f"Failed to refresh board_config for {board_id[:8]}: {exc}",
                            RNS.LOG_WARNING,
                        )

                self._sync_engine._loop.call_soon_threadsafe(
                    lambda: self._sync_engine._loop.create_task(_refresh_config())
                )
            else:
                # Engine not started yet (startup announce replay).
                # Kick off a one-shot event loop for the DB write.
                import asyncio
                try:
                    asyncio.get_event_loop().run_until_complete(
                        self._subscribe_board(board)
                    )
                except RuntimeError:
                    # Already in an event loop — this shouldn't happen at
                    # startup but handle it gracefully.
                    RNS.log(
                        f"Could not refresh board_config for {board_id[:8]} "
                        f"(event loop busy)",
                        RNS.LOG_WARNING,
                    )

        # v3.6.2 §5.2: Register peer using peer_lxmf_hash from announce.
        if self._sync_engine and board.peer_lxmf_hash:
            self._sync_engine.peer_tracker.register_from_announce(
                board_id=board_id,
                peer_lxmf_hash=board.peer_lxmf_hash,
                identity=announced_identity,
                announce_hash=board_id,  # board dest hash for cross-reference
            )
            self._sync_engine._request_path(board.peer_lxmf_hash)
            RNS.log(
                f"Board discover: '{board.display_name}' ({board_id[:8]}) "
                f"peer_lxmf={board.peer_lxmf_hash[:16]}",
                RNS.LOG_INFO,
            )
        elif self._sync_engine:
            # Legacy v3.6.1 announce — no peer_lxmf_hash. Register with
            # board hash as fallback (broadcast-only, §10.2).
            self._sync_engine.peer_tracker.see_peer(
                board_id, bytes.fromhex(board_id), announced_identity,
            )
            RNS.log(
                f"Board discover (legacy v1): '{board.display_name}' ({board_id[:8]})",
                RNS.LOG_INFO,
            )
        else:
            RNS.log(
                f"Board discover: '{board.display_name}' ({board_id[:8]})",
                RNS.LOG_INFO,
            )

    def _on_identity_announce_received(
        self,
        peer_lxmf_hash: str,
        announced_identity: RNS.Identity,
        board_ids: list,
    ) -> None:
        """
        Callback invoked when an LXMF identity announce from a RetiBoard
        peer is received (§8.2, §9.2).

        v3.6.3: Identity announces no longer contain the list of boards to
        avoid RNS MTU limits. Instead, we send a P2P LXMF message
        (MSG_TYPE_BOARD_LIST_REQ) to request their board list.
        """
        if self._sync_engine is None:
            return

        # Don't register ourselves.
        self_hash = self._sync_engine.get_lxmf_hash()
        if peer_lxmf_hash == self_hash:
            return

        # We must request the path to the peer so we can route the LXMF message.
        self._sync_engine._request_path(peer_lxmf_hash)

        # Send a board list request via P2P LXMF.
        from retiboard.sync import MSG_TYPE_BOARD_LIST_REQ
        from retiboard.sync.message_queue import Priority
        import json
        request_payload = json.dumps({}).encode("utf-8")
        
        self._sync_engine.send_lxmf(
            peer_lxmf_hash,
            request_payload,
            MSG_TYPE_BOARD_LIST_REQ,
            Priority.CONTROL,
        )

        RNS.log(
            f"Requested board list from peer {peer_lxmf_hash[:16]} "
            f"discovered via LXMF identity announce",
            RNS.LOG_INFO,
        )

        # We must ensure the peer is known for routing in the meantime.
        self._sync_engine.peer_tracker.register_peer_identity(
            peer_lxmf_hash=peer_lxmf_hash,
            identity=announced_identity,
        )

    def _on_board_list_received(
        self,
        peer_lxmf_hash: str,
        board_ids: list,
    ) -> None:
        """
        Callback invoked when a peer responds with their board list via P2P LXMF.
        This handles peer discovery, cold-start board pushes, and catch-up sync.
        """
        if self._sync_engine is None:
            return

        # Only register the peer for boards we are actually subscribed to.
        subscribed_ids = set()
        if BOARDS_DIR.exists():
            subscribed_ids = {
                d.name for d in BOARDS_DIR.iterdir()
                if d.is_dir() and (d / "meta.db").exists()
            }

        # Don't register ourselves.
        self_hash = self._sync_engine.get_lxmf_hash()
        if peer_lxmf_hash == self_hash:
            return

        registered_any = False
        shared_boards = []
        for bid in board_ids:
            self._record_discovered_peer_advertisement(
                bid,
                peer_lxmf_hash,
            )
            if bid in subscribed_ids:
                # Identity was stored during the initial identity announce
                identity = self._sync_engine.peer_tracker.get_peer_identity(peer_lxmf_hash)
                if identity:
                    self._sync_engine.peer_tracker.register_from_announce(
                        board_id=bid,
                        peer_lxmf_hash=peer_lxmf_hash,
                        identity=identity,
                    )
                self._sync_engine._request_path(peer_lxmf_hash)
                registered_any = True
                shared_boards.append(bid)

        if registered_any:
            RNS.log(
                f"Peer {peer_lxmf_hash[:16]} shares {len(shared_boards)} board(s)",
                RNS.LOG_INFO,
            )
            for bid in shared_boards:
                self._sync_engine.schedule_catchup(bid)

        # -----------------------------------------------------------------
        # Cold-start board announce push (fixes missed-announce race).
        # -----------------------------------------------------------------
        owned_board_ids = set(self._owned_destinations.keys())
        peer_board_ids = set(board_ids)
        missing_from_peer = owned_board_ids - peer_board_ids

        if missing_from_peer:
            sent = self._sync_engine.send_board_announces_to_peer(
                peer_lxmf_hash,
            )
            if sent > 0:
                RNS.log(
                    f"Pushed {sent} owned board announce(s) to new peer "
                    f"{peer_lxmf_hash[:16]} (they were missing "
                    f"{len(missing_from_peer)} board(s))",
                    RNS.LOG_INFO,
                )

        all_subscribed_ids = set()
        if BOARDS_DIR.exists():
            all_subscribed_ids = {
                d.name for d in BOARDS_DIR.iterdir()
                if d.is_dir() and (d / "meta.db").exists()
            }
        subscribed_not_owned = all_subscribed_ids - owned_board_ids
        sub_missing = subscribed_not_owned - peer_board_ids

        if sub_missing:
            import json
            from retiboard.boards.subscribe import load_announce_cache
            from retiboard.sync import MSG_TYPE_BOARD_ANNOUNCE
            from retiboard.sync.message_queue import Priority

            sub_sent = 0
            for bid in list(sub_missing)[:10]:
                announce_dict = load_announce_cache(bid)
                if announce_dict is None:
                    board = self._announce_handler.received_announces.get(bid)
                    if board:
                        announce_dict = board.to_announce_dict()

                if announce_dict is None:
                    continue

                payload = json.dumps(
                    announce_dict, separators=(",", ":")
                ).encode("utf-8")
                ok = self._sync_engine.send_lxmf(
                    peer_lxmf_hash, payload,
                    MSG_TYPE_BOARD_ANNOUNCE, Priority.CONTROL,
                )
                if ok:
                    sub_sent += 1

            if sub_sent > 0:
                RNS.log(
                    f"Pushed {sub_sent} subscribed board announce(s) to peer "
                    f"{peer_lxmf_hash[:16]}",
                    RNS.LOG_INFO,
                )
