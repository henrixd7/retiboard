"""
Board announce creation and broadcast over RNS.

Spec references:
    §3.3  — Board announce schema (JSON payload in app_data)
    §5    — key_material distribution via announce; threat model
    §9    — Board discovery: RNS announce propagation + rns://board/<board_id>
    §12.1 — Identity signing (announce is signed by the creator's identity)

RNS API (verified against reticulum.network/manual/reference.html):
    - RNS.Destination(identity, direction, type, app_name, *aspects)
    - destination.announce(app_data=bytes)  — broadcast signed announce
    - RNS.Transport.register_announce_handler(handler)
    - Handler needs: aspect_filter attribute, received_announce() method
    - destination.hash — bytes, the destination hash
    - destination.hexhash — hex string of the hash

Design:
    Each board has an RNS Destination in the "retiboard.board" app space.
    The board_id IS the destination hash (hexhash). When you create a board,
    you create a Destination with your identity, and announce it with the
    §3.3 JSON schema as app_data. Other nodes receive the announce via a
    registered handler, validate the signature (automatic in RNS), parse
    the app_data, and can subscribe.

    The announce is signed by the board creator's identity automatically
    by RNS — Destination.announce() handles signing internally.

    key_material is included in the app_data. This is intentional (§5):
    "Anyone who obtains the announce packet can derive the AES-GCM board key."
    The backend never derives or uses the key — only passes it through.
"""

from __future__ import annotations

import json
from typing import Optional, Callable

import RNS

from retiboard.db.models import Board


# RNS app namespace for RetiBoard board destinations.
# Destination name format: "retiboard.board"
# Full destination: hash(identity + "retiboard" + "board")
APP_NAME = "retiboard"
BOARD_ASPECT = "board"


def create_board_destination(
    identity: RNS.Identity,
    board_unique_id: str | None = None,
) -> RNS.Destination:
    """
    Create an RNS Destination for a board owned by the given identity.

    The destination is SINGLE type (one-to-one addressable) and IN
    direction (we are the owner/responder).

    The destination hash becomes the board_id. To ensure each board
    gets a UNIQUE hash even when created by the same identity, we
    append a random unique identifier as an additional aspect.

    RNS destination hash = hash(identity + app_name + aspects), so
    different aspects → different hash → different board_id.

    Args:
        identity: The board creator's RNS identity.
        board_unique_id: Optional unique string for this board.
                         If None, a random one is generated.

    Returns:
        RNS.Destination configured for board announces.
    """
    import secrets
    if board_unique_id is None:
        board_unique_id = secrets.token_hex(16)

    destination = RNS.Destination(
        identity,
        RNS.Destination.IN,
        RNS.Destination.SINGLE,
        APP_NAME,
        BOARD_ASPECT,
        board_unique_id,
    )
    RNS.log(
        f"Created board destination: {destination.hexhash}",
        RNS.LOG_DEBUG,
    )
    return destination


def build_announce_data(board: Board) -> bytes:
    """
    Serialize a Board's announce schema (§3.3) to bytes for app_data.

    The announce dict includes key_material — this is intentional per §5.
    It's public data distributed via the announce.

    Returns:
        UTF-8 encoded JSON bytes of the announce payload.
    """
    announce_dict = board.to_announce_dict()
    return json.dumps(announce_dict, separators=(",", ":")).encode("utf-8")


def broadcast_announce(
    destination: RNS.Destination,
    board: Board,
) -> bool:
    """
    Broadcast a board announce on the RNS network.

    Returns True on success, False on failure (e.g., MTU exceeded).
    """
    app_data = build_announce_data(board)

    RNS.log(
        f"Broadcasting announce for board '{board.display_name}' "
        f"({destination.hexhash}), {len(app_data)} bytes app_data",
        RNS.LOG_INFO,
    )

    try:
        destination.announce(app_data=app_data)
        return True
    except IOError as e:
        RNS.log(
            f"Announce failed (packet too large): {e}. "
            f"app_data={len(app_data)} bytes. "
            f"Try a shorter board name.",
            RNS.LOG_ERROR,
        )
        return False


def parse_announce_data(app_data: Optional[bytes]) -> Optional[dict]:
    """
    Parse raw announce app_data bytes into a board announce dict.

    Returns None if app_data is missing or malformed.
    Does NOT validate field completeness — caller should check required fields.
    """
    if app_data is None:
        return None
    try:
        data = json.loads(app_data.decode("utf-8"))
        if not isinstance(data, dict):
            return None
        return data
    except (json.JSONDecodeError, UnicodeDecodeError):
        RNS.log("Failed to parse announce app_data as JSON", RNS.LOG_DEBUG)
        return None


def validate_announce_fields(data: dict) -> bool:
    """
    Validate that an announce dict contains required fields.

    Supports both compact (v3.6.2: "b", "n", "km") and
    verbose (v3.6.1: "board_id", "display_name", "key_material") formats.
    """
    # Compact format check.
    if "b" in data:
        if not isinstance(data["b"], str) or not data["b"]:
            RNS.log("Announce has empty board_id (compact)", RNS.LOG_WARNING)
            return False
        if "km" not in data:
            RNS.log("Announce missing key_material (compact)", RNS.LOG_WARNING)
            return False
        return True

    # Verbose format check.
    required = {"board_id", "display_name", "key_material"}
    if not required.issubset(data.keys()):
        missing = required - data.keys()
        RNS.log(f"Announce missing required fields: {missing}", RNS.LOG_WARNING)
        return False
    if not isinstance(data["board_id"], str) or not data["board_id"]:
        RNS.log("Announce has empty or invalid board_id", RNS.LOG_WARNING)
        return False
    return True


def get_board_id_from_announce(data: dict) -> str:
    """Extract board_id from either compact or verbose announce format."""
    return data.get("b", data.get("board_id", ""))


# =============================================================================
# Announce handler (receiver side)
# =============================================================================

class BoardAnnounceHandler:
    """
    RNS announce handler for RetiBoard board discovery.

    Registered with RNS.Transport.register_announce_handler(). When a
    board announce arrives on the network, RNS calls received_announce()
    with the destination hash, the announced identity, and the app_data.

    RNS API contract:
        - Must have an `aspect_filter` attribute (string or None).
        - Must have a `received_announce(destination_hash, announced_identity, app_data)` method.

    The announce signature is already verified by RNS before this handler
    is called — we don't need to verify it ourselves (§12.1).

    This handler processes three types of announces:
        1. Board announces (§3.3) — board discovery
        2. LXMF identity announces (§8.2) — peer discovery
        3. HAVE announcements (§7.1 Tier 2) — piggybacked on board re-announces

    This handler calls a user-supplied callback with the parsed Board
    object so the manager can decide whether to subscribe.
    """

    def __init__(
        self,
        on_announce: Optional[Callable[[str, RNS.Identity, Board], None]] = None,
        on_identity_announce: Optional[Callable[[str, RNS.Identity, list], None]] = None,
    ):
        """
        Args:
            on_announce: Callback invoked when a valid board announce is received.
                         Signature: (destination_hexhash, announced_identity, board) -> None
            on_identity_announce: Callback invoked when an LXMF identity announce
                         is received from a RetiBoard peer (§8.2, §9.2).
                         Signature: (peer_lxmf_hash, identity, board_ids) -> None
        """
        # aspect_filter=None receives ALL announces. We filter ourselves
        # by parsing app_data for valid RetiBoard board announce JSON.
        # This is necessary because each board has a unique random aspect
        # (retiboard.board.<unique_id>), so we can't pre-register a
        # specific filter that matches all boards.
        self.aspect_filter = None
        self._on_announce = on_announce
        self._on_identity_announce = on_identity_announce

        # Cache of received announces: {board_id: Board}
        # This is the in-memory announce cache. key_material lives here
        # (in memory), never written to SQLite.
        self.received_announces: dict[str, Board] = {}

    def received_announce(
        self,
        destination_hash: bytes,
        announced_identity: RNS.Identity,
        app_data: Optional[bytes],
    ) -> None:
        """
        Called by RNS Transport when a matching announce arrives.

        The announce is already signature-verified by RNS at this point.
        We parse the app_data, validate fields, cache it, and notify
        the callback if set.

        Handles three announce types:
            1. LXMF identity announces (§8.2): {"app":"retiboard","boards":[...],"version":"3.6.2"}
            2. HAVE piggybacked on board re-announces (§7.1 Tier 2): {"active_threads":[...]}
            3. Board announces (§3.3): {"board_id":...,"display_name":...,"key_material":...}
        """
        dest_hex = RNS.prettyhexrep(destination_hash)

        data = parse_announce_data(app_data)
        if data is None:
            # Not JSON app_data — not a RetiBoard announce, ignore silently.
            return

        # -------------------------------------------------------------------
        # Type 1: LXMF identity announce (§8.2)
        #
        # These are periodic announces from RetiBoard nodes advertising
        # their LXMF delivery destination and the boards they participate in.
        # The destination_hash here is the peer's LXMF delivery hash.
        #
        # §9.2: "From LXMF messages and identity announces, learn peer info."
        # These are a critical peer discovery source — without them, a board
        # creator cannot learn about subscribers until they send LXMF.
        # -------------------------------------------------------------------
        if data.get("app") == "retiboard":
            peer_lxmf_hash = destination_hash.hex()
            boards = data.get("boards", [])
            version = data.get("version", "?")

            RNS.log(
                f"Received LXMF identity announce from {peer_lxmf_hash[:16]} "
                f"(v{version})",
                RNS.LOG_DEBUG,
            )

            if self._on_identity_announce:
                self._on_identity_announce(
                    peer_lxmf_hash, announced_identity, boards,
                )
            return

        # -------------------------------------------------------------------
        # Type 2: HAVE announcement piggybacked on board re-announce (§7.1 Tier 2)
        #
        # HAVE packets have "active_threads" key; board announces don't.
        # The destination_hash here is the BOARD destination hash (not peer LXMF).
        # We need to look up the actual peer_lxmf_hash for this board owner
        # so the delta request targets the correct LXMF delivery destination.
        # -------------------------------------------------------------------
        if "active_threads" in data and "board_id" in data:
            RNS.log(
                f"Received HAVE via announce for board {data.get('board_id', '?')[:8]}",
                RNS.LOG_DEBUG,
            )
            try:
                from retiboard.sync.have_handler import handle_have_announcement

                # The destination_hash here is the board dest hash, NOT the
                # peer's LXMF hash. The have_handler will look up the correct
                # peer via the board_id and the announced_identity.
                #
                # This callback runs on the RNS transport thread, not in
                # the asyncio event loop. We need to get the engine's loop
                # to schedule the async work thread-safely.
                from retiboard.sync.have_handler import _sync_engine
                if _sync_engine and _sync_engine._loop and not _sync_engine._loop.is_closed():
                    def _schedule():
                        _sync_engine._loop.create_task(handle_have_announcement(
                            app_data,
                            source_hash=destination_hash,
                            source_identity=announced_identity,
                            is_from_board_announce=True,
                        ))
                    _sync_engine._loop.call_soon_threadsafe(_schedule)
                else:
                    RNS.log(
                        "HAVE via board announce dropped: engine loop unavailable",
                        RNS.LOG_WARNING,
                    )
            except Exception as e:
                RNS.log(f"Error processing HAVE from announce: {e}", RNS.LOG_DEBUG)
            return

        # -------------------------------------------------------------------
        # Type 3: Board announce (§3.3)
        # -------------------------------------------------------------------
        RNS.log(
            f"Received board announce from {dest_hex}",
            RNS.LOG_INFO,
        )

        if not validate_announce_fields(data):
            RNS.log(
                f"Ignoring announce from {dest_hex}: invalid fields",
                RNS.LOG_WARNING,
            )
            return

        # Verify that the board_id in the payload matches the destination hash.
        actual_hex = destination_hash.hex()
        claimed_id = get_board_id_from_announce(data)
        if claimed_id != actual_hex:
            RNS.log(
                f"Announce board_id mismatch: claimed {claimed_id}, "
                f"actual destination {actual_hex}. Rejecting.",
                RNS.LOG_WARNING,
            )
            return

        board = Board.from_announce_dict(data)

        # Cache in memory (key_material stays in RAM only).
        self.received_announces[board.board_id] = board

        RNS.log(
            f"Valid board announce: '{board.display_name}' ({board.board_id})",
            RNS.LOG_INFO,
        )

        if self._on_announce:
            self._on_announce(board.board_id, announced_identity, board)
