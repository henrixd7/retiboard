from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from retiboard.sync.peers import PeerTracker
from retiboard.sync.receiver import _resolve_source_identity


class _DummyIdentity:
    def __init__(self, hash_value: bytes = b"peerhash") -> None:
        self.hash = hash_value


def test_resolve_source_identity_falls_back_to_peer_tracker_when_rns_recall_is_cold() -> None:
    peer_tracker = PeerTracker()
    identity = _DummyIdentity()
    board_id = "96b7ce9e7c4613324c77872bbc3e0791"
    peer_lxmf_hash = "91420f3ebdd0bb96"

    peer_tracker.register_from_announce(
        board_id=board_id,
        peer_lxmf_hash=peer_lxmf_hash,
        identity=identity,
    )

    message = SimpleNamespace(
        source=None,
        source_hash=bytes.fromhex(peer_lxmf_hash),
    )

    with patch("retiboard.sync.receiver.RNS.Identity.recall", return_value=None):
        resolved = _resolve_source_identity(message, peer_tracker)

    assert resolved is identity
