from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from retiboard.db.models import Board
from retiboard.boards import manager as board_manager_module
from retiboard.boards.manager import (
    BoardManager,
    DISCOVERED_BOARD_STALE_SECONDS,
)
from retiboard.sync.peers import PeerTracker


def _make_board(board_id: str, name: str, owner_hash: str) -> Board:
    return Board(
        board_id=board_id,
        display_name=name,
        key_material="ab" * 32,
        announce_version=2,
        peer_lxmf_hash=owner_hash,
    )


class _PeerStub:
    def __init__(self, *, verified: bool) -> None:
        self.verified = verified

    def is_expired(self, now=None) -> bool:
        return False


def test_peer_tracker_unique_peer_count_deduplicates_cross_board_membership() -> None:
    tracker = PeerTracker()
    peer_hash = "11" * 8
    other_hash = "22" * 8

    tracker.register_from_announce("board-a", peer_hash)
    tracker.register_from_announce("board-b", peer_hash)
    tracker.register_from_announce("board-b", other_hash)

    assert tracker.peer_count("board-a") == 1
    assert tracker.peer_count("board-b") == 2
    assert tracker.unique_peer_count(["board-a", "board-b"]) == 2


def test_discovered_board_snapshots_track_peer_advertisements_and_prune_stale_entries(tmp_path: Path) -> None:
    with patch.object(board_manager_module, "BOARDS_DIR", tmp_path), patch(
        "retiboard.boards.manager.RNS.Transport.register_announce_handler",
        return_value=None,
    ):
        manager = BoardManager(identity=object())

        tracker = SimpleNamespace(
            get_peer=lambda peer_hash: {
                "aa" * 8: _PeerStub(verified=True),
                "bb" * 8: _PeerStub(verified=False),
            }.get(peer_hash)
        )
        manager.set_sync_engine(SimpleNamespace(peer_tracker=tracker))

        now = time.time()
        board = _make_board("be" * 16, "General", "aa" * 8)

        manager._record_discovered_board(board, now=now)
        manager._record_discovered_peer_advertisement(
            board.board_id,
            "bb" * 8,
            now=now + 30,
        )

        snapshots = manager.get_discovered_boards()
        assert len(snapshots) == 1
        assert snapshots[0].name_key == "general"
        assert snapshots[0].announce_seen_count == 1
        assert snapshots[0].advertising_peer_count == 2
        assert snapshots[0].verified_peer_count == 1

        manager._prune_discovered_records(
            now=now + 30 + DISCOVERED_BOARD_STALE_SECONDS + 1,
        )
        assert manager._discovered_boards == {}
        assert manager._announce_handler.received_announces == {}
