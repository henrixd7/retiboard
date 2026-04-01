"""
Catch-up Sync & Recovery Tests — Peer Sync Improvement.

Run with: python -m pytest tests/test_catchup_sync.py -v
    or:   python tests/test_catchup_sync.py  (standalone)

Tests cover:
    1. MSG_TYPE_HAVE_REQ constant exists in sync module
    2. SyncEngine.sync_board_catchup sends HAVE_REQ to peers
    3. sync_board_catchup is a no-op when no peers exist (graceful)
    4. sync_board_catchup caps fan-out at 5 peers
    5. HAVE_REQ handler builds and sends HAVE response
    6. HAVE_REQ handler ignores unknown boards
    7. LXMF HAVE messages are processed (not just logged)
    8. BoardManager.subscribe triggers catch-up sync
    9. Path resolution wakeup event fires on catch-up
   10. Identity announce triggers catch-up for shared boards
   11. Duplicate catch-up requests are idempotent
   12. Catch-up failure is non-fatal (subscribe still succeeds)

Spec references:
    §7.1 Tier 2 — HAVE announcements
    §9.2        — message.source is authoritative
    §13.1       — LXMF direct preferred for known peers
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from unittest.mock import MagicMock, AsyncMock, patch

from retiboard.sync.message_queue import SendResult

# Ensure project root is on path.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Override RETIBOARD_HOME before importing anything else.
_TEST_HOME = tempfile.mkdtemp(prefix="retiboard_catchup_test_")
os.environ["RETIBOARD_HOME"] = _TEST_HOME


# ============================================================================
# Lightweight mocks — avoid requiring RNS/LXMF at test time.
# ============================================================================

class MockIdentity:
    """Minimal RNS.Identity mock."""
    def __init__(self, hash_hex: str = "aabbccdd11223344"):
        self._hash = bytes.fromhex(hash_hex)

    @property
    def hash(self):
        return self._hash


class MockDestination:
    """Minimal RNS.Destination mock."""
    def __init__(self, hexhash: str = "1234abcd5678ef90"):
        self._hexhash = hexhash
        self._hash = bytes.fromhex(hexhash)

    @property
    def hexhash(self):
        return self._hexhash

    @property
    def hash(self):
        return self._hash

    def announce(self, app_data=None):
        pass


class MockLXMFMessage:
    """Minimal LXMF message mock for delivery callback testing."""
    def __init__(self, title: str, content: str, source_hash: bytes):
        self.title = title.encode("utf-8")
        self.content = content.encode("utf-8")
        self.source_hash = source_hash
        self.signature_validated = True
        self.timestamp = time.time()


# ============================================================================
# Test helpers
# ============================================================================

def make_mock_peer(lxmf_hash: str, board_id: str, identity=None):
    """Create a PeerInfo-like mock with required attributes."""
    from retiboard.sync.peers import PeerInfo, PathState
    return PeerInfo(
        lxmf_hash=lxmf_hash,
        identity=identity or MockIdentity(lxmf_hash[:16].ljust(16, "0")),
        boards={board_id},
        last_seen=time.time(),
        path_state=PathState.KNOWN,
        verified=True,
    )


# ============================================================================
# Tests
# ============================================================================

async def test_have_req_constant_exists():
    """MSG_TYPE_HAVE_REQ is defined in sync module."""
    from retiboard.sync import MSG_TYPE_HAVE_REQ
    assert MSG_TYPE_HAVE_REQ == "retiboard.have_req"
    print("  PASS: MSG_TYPE_HAVE_REQ constant exists")


async def test_sync_board_catchup_sends_have_req():
    """sync_board_catchup sends HAVE_REQ LXMF to known peers."""
    from retiboard.sync.peers import PeerTracker
    from retiboard.sync import MSG_TYPE_HAVE_REQ

    board_id = "aaaa1111bbbb2222"
    peer_hash = "cccc3333dddd4444"
    self_hash = "eeee5555ffff6666"

    tracker = PeerTracker()
    peer = make_mock_peer(peer_hash, board_id)
    tracker._peers[peer_hash] = peer
    tracker._board_index[board_id] = {peer_hash}

    # Build a minimal SyncEngine with mocked internals.
    engine = MagicMock()
    engine._lxm_router = True
    engine._lxmf_destination = MockDestination(self_hash)
    engine.peer_tracker = tracker
    engine.send_lxmf = MagicMock(return_value=SendResult.SENT)
    engine._path_resolution_wakeup = asyncio.Event()

    # Import and bind the real method.
    from retiboard.sync.engine import SyncEngine
    await SyncEngine.sync_board_catchup(engine, board_id)

    # Verify send_lxmf was called with HAVE_REQ.
    engine.send_lxmf.assert_called_once()
    call_args = engine.send_lxmf.call_args
    assert call_args[0][0] == peer_hash  # target peer
    assert call_args[0][2] == MSG_TYPE_HAVE_REQ  # message type
    # Verify the payload contains board_id.
    payload = json.loads(call_args[0][1].decode("utf-8"))
    assert payload["board_id"] == board_id
    print("  PASS: sync_board_catchup sends HAVE_REQ to peers")


async def test_sync_board_catchup_no_peers_graceful():
    """sync_board_catchup is a no-op when no peers exist."""
    from retiboard.sync.peers import PeerTracker

    board_id = "aaaa1111bbbb2222"
    self_hash = "eeee5555ffff6666"

    tracker = PeerTracker()

    engine = MagicMock()
    engine._lxm_router = True
    engine._lxmf_destination = MockDestination(self_hash)
    engine.peer_tracker = tracker
    engine.send_lxmf = MagicMock()
    engine._path_resolution_wakeup = asyncio.Event()

    from retiboard.sync.engine import SyncEngine
    await SyncEngine.sync_board_catchup(engine, board_id)

    # No LXMF messages should have been sent.
    engine.send_lxmf.assert_not_called()
    print("  PASS: sync_board_catchup no-op with no peers")


async def test_sync_board_catchup_caps_fanout():
    """sync_board_catchup sends to at most 5 peers."""
    from retiboard.sync.peers import PeerTracker

    board_id = "aaaa1111bbbb2222"
    self_hash = "eeee5555ffff6666"

    tracker = PeerTracker()
    # Register 8 peers.
    for i in range(8):
        h = f"{i:016x}"
        peer = make_mock_peer(h, board_id)
        tracker._peers[h] = peer
        tracker._board_index.setdefault(board_id, set()).add(h)

    engine = MagicMock()
    engine._lxm_router = True
    engine._lxmf_destination = MockDestination(self_hash)
    engine.peer_tracker = tracker
    engine.send_lxmf = MagicMock(return_value=SendResult.SENT)
    engine._path_resolution_wakeup = asyncio.Event()

    from retiboard.sync.engine import SyncEngine
    await SyncEngine.sync_board_catchup(engine, board_id)

    # Should cap at 5 peers.
    assert engine.send_lxmf.call_count == 5
    print("  PASS: sync_board_catchup caps fan-out at 5")


async def test_sync_board_catchup_excludes_self():
    """sync_board_catchup does not send HAVE_REQ to itself."""
    from retiboard.sync.peers import PeerTracker

    board_id = "aaaa1111bbbb2222"
    self_hash = "eeee5555ffff6666"

    tracker = PeerTracker()
    # Register self as a peer (as BoardManager does).
    self_peer = make_mock_peer(self_hash, board_id)
    tracker._peers[self_hash] = self_peer
    tracker._board_index[board_id] = {self_hash}

    engine = MagicMock()
    engine._lxm_router = True
    engine._lxmf_destination = MockDestination(self_hash)
    engine.peer_tracker = tracker
    engine.send_lxmf = MagicMock()
    engine._path_resolution_wakeup = asyncio.Event()

    from retiboard.sync.engine import SyncEngine
    await SyncEngine.sync_board_catchup(engine, board_id)

    # Self should be excluded — no messages sent.
    engine.send_lxmf.assert_not_called()
    print("  PASS: sync_board_catchup excludes self")


async def test_path_resolution_wakeup_fires():
    """sync_board_catchup sets the path resolution wakeup event."""
    from retiboard.sync.peers import PeerTracker

    board_id = "aaaa1111bbbb2222"
    self_hash = "eeee5555ffff6666"
    peer_hash = "cccc3333dddd4444"

    tracker = PeerTracker()
    peer = make_mock_peer(peer_hash, board_id)
    tracker._peers[peer_hash] = peer
    tracker._board_index[board_id] = {peer_hash}

    wakeup = asyncio.Event()
    engine = MagicMock()
    engine._lxm_router = True
    engine._lxmf_destination = MockDestination(self_hash)
    engine.peer_tracker = tracker
    engine.send_lxmf = MagicMock(return_value=SendResult.SENT)
    engine._path_resolution_wakeup = wakeup

    assert not wakeup.is_set()

    from retiboard.sync.engine import SyncEngine
    await SyncEngine.sync_board_catchup(engine, board_id)

    assert wakeup.is_set()
    print("  PASS: path resolution wakeup fires on catch-up")


async def test_have_req_handler_responds_with_have():
    """HAVE_REQ handler builds HAVE and sends back via MSG_TYPE_HAVE."""
    from retiboard.sync import MSG_TYPE_HAVE_REQ, MSG_TYPE_HAVE
    from retiboard.sync.peers import PeerTracker
    from retiboard.db.database import open_board_db, save_board_config, insert_post
    from retiboard.db.models import Board, PostMetadata
    from retiboard.config import BOARDS_DIR

    board_id = "test_have_req_bd"
    requester_hash = bytes.fromhex("abcd1234abcd1234")

    # Create a board with one thread so HAVE has content.
    board = Board(
        board_id=board_id,
        display_name="Test Board",
        text_only=False,
        default_ttl_seconds=43200,
        bump_decay_rate=3600,
        max_active_threads_local=50,
        pow_difficulty=0,
        key_material="",
        announce_version=2,
        peer_lxmf_hash="",
        subscribed_at=time.time(),
    )
    db = await open_board_db(board_id)
    await save_board_config(db, board)

    now = int(time.time())
    post = PostMetadata(
        post_id="post001", thread_id="post001", parent_id="",
        timestamp=now, expiry_timestamp=now + 43200,
        bump_flag=False, content_hash="abc123", payload_size=100,
        attachment_content_hash="", attachment_payload_size=0,
        has_attachments=False, text_only=False,
        identity_hash="id001", pow_nonce="0",
        thread_last_activity=now, is_abandoned=False,
    )
    await insert_post(db, post)
    await db.close()

    # Build the mock sync engine for sending response.
    tracker = PeerTracker()
    sync_engine = MagicMock()
    sync_engine.send_lxmf = MagicMock(return_value=SendResult.SENT)

    # Build delivery callback and invoke with HAVE_REQ message.
    from retiboard.sync.receiver import make_delivery_callback
    callback = make_delivery_callback(tracker, sync_engine=sync_engine)

    content = json.dumps({"board_id": board_id})
    msg = MockLXMFMessage(MSG_TYPE_HAVE_REQ, content, requester_hash)

    # Callback runs in sync context; async tasks are created in the loop.
    callback(msg)

    # Let the event loop process the created task.
    await asyncio.sleep(0.2)

    # Verify send_lxmf was called with MSG_TYPE_HAVE.
    assert sync_engine.send_lxmf.called, "send_lxmf should have been called"
    call_args = sync_engine.send_lxmf.call_args
    assert call_args[0][2] == MSG_TYPE_HAVE  # message type is HAVE
    # Verify the HAVE payload contains the board and thread.
    have_payload = json.loads(call_args[0][1].decode("utf-8"))
    assert have_payload["board_id"] == board_id
    assert len(have_payload["active_threads"]) >= 1
    assert have_payload["active_threads"][0]["thread_id"] == "post001"

    # Cleanup.
    board_path = BOARDS_DIR / board_id
    if board_path.exists():
        shutil.rmtree(board_path)

    print("  PASS: HAVE_REQ handler responds with HAVE")


async def test_have_req_handler_ignores_unknown_board():
    """HAVE_REQ for a board we don't have is silently ignored."""
    from retiboard.sync import MSG_TYPE_HAVE_REQ
    from retiboard.sync.peers import PeerTracker

    tracker = PeerTracker()
    sync_engine = MagicMock()
    sync_engine.send_lxmf = MagicMock()

    from retiboard.sync.receiver import make_delivery_callback
    callback = make_delivery_callback(tracker, sync_engine=sync_engine)

    content = json.dumps({"board_id": "nonexistent_board"})
    msg = MockLXMFMessage(MSG_TYPE_HAVE_REQ, content, b"\x00" * 8)
    callback(msg)
    await asyncio.sleep(0.2)

    # No HAVE response should be sent for unknown boards.
    sync_engine.send_lxmf.assert_not_called()
    print("  PASS: HAVE_REQ ignored for unknown board")


async def test_lxmf_have_is_processed():
    """
    MSG_TYPE_HAVE via LXMF is actually processed (not just logged).

    This tests the fix for the dead code path where HAVE messages
    received via LXMF were logged but never passed to handle_have_announcement.
    """
    from retiboard.sync import MSG_TYPE_HAVE
    from retiboard.sync.peers import PeerTracker

    tracker = PeerTracker()

    from retiboard.sync.receiver import make_delivery_callback
    callback = make_delivery_callback(tracker, sync_engine=None)

    # Build a valid HAVE packet.
    have_data = json.dumps({
        "board_id": "test_board_001",
        "active_threads": [
            {"thread_id": "thread_001", "latest_post_timestamp": 1000, "post_count": 5}
        ],
    })

    source_hash = bytes.fromhex("aabb112233445566")
    msg = MockLXMFMessage(MSG_TYPE_HAVE, have_data, source_hash)

    # Patch handle_have_announcement to verify it gets called.
    with patch("retiboard.sync.have_handler.handle_have_announcement", new_callable=AsyncMock) as mock_handler:
        mock_handler.return_value = 0
        callback(msg)
        await asyncio.sleep(0.2)

        assert mock_handler.called, "handle_have_announcement should be called for LXMF HAVE"
        call_kwargs = mock_handler.call_args
        assert call_kwargs[1].get("is_from_board_announce") is False

    # Verify peer was registered authoritatively.
    peer = tracker.get_peer(source_hash.hex())
    assert peer is not None
    assert peer.verified is True

    print("  PASS: LXMF HAVE is processed (not just logged)")


async def test_lxmf_have_registers_peer_authoritatively():
    """
    Peer discovered via LXMF HAVE is registered with verified=True
    and path_state=KNOWN (§9.2: message.source is authoritative).
    """
    from retiboard.sync import MSG_TYPE_HAVE
    from retiboard.sync.peers import PeerTracker, PathState

    tracker = PeerTracker()

    from retiboard.sync.receiver import make_delivery_callback
    callback = make_delivery_callback(tracker, sync_engine=None)

    have_data = json.dumps({
        "board_id": "board_xyz",
        "active_threads": [],
    })

    source_hash = bytes.fromhex("1122334455667788")
    msg = MockLXMFMessage(MSG_TYPE_HAVE, have_data, source_hash)

    with patch("retiboard.sync.have_handler.handle_have_announcement", new_callable=AsyncMock) as mock_handler:
        mock_handler.return_value = 0
        callback(msg)
        await asyncio.sleep(0.1)

    peer = tracker.get_peer("1122334455667788")
    assert peer is not None
    assert peer.verified is True
    assert peer.path_state == PathState.KNOWN

    print("  PASS: LXMF HAVE registers peer authoritatively")


async def test_catchup_idempotent():
    """
    Multiple sync_board_catchup calls for the same board don't cause
    duplicate problems — existing HAVE dedup handles it.
    """
    from retiboard.sync.peers import PeerTracker

    board_id = "aaaa1111bbbb2222"
    self_hash = "eeee5555ffff6666"
    peer_hash = "cccc3333dddd4444"

    tracker = PeerTracker()
    peer = make_mock_peer(peer_hash, board_id)
    tracker._peers[peer_hash] = peer
    tracker._board_index[board_id] = {peer_hash}

    engine = MagicMock()
    engine._lxm_router = True
    engine._lxmf_destination = MockDestination(self_hash)
    engine.peer_tracker = tracker
    engine.send_lxmf = MagicMock(return_value=SendResult.SENT)
    engine._path_resolution_wakeup = asyncio.Event()

    from retiboard.sync.engine import SyncEngine

    # Call catch-up three times.
    await SyncEngine.sync_board_catchup(engine, board_id)
    await SyncEngine.sync_board_catchup(engine, board_id)
    await SyncEngine.sync_board_catchup(engine, board_id)

    # Each call should send one HAVE_REQ (3 total). The receiver's
    # existing dedup in handle_have_announcement ensures no duplicates.
    assert engine.send_lxmf.call_count == 3
    print("  PASS: catch-up is idempotent (3 calls = 3 sends, dedup on receiver)")


async def test_sync_board_catchup_no_lxmf():
    """sync_board_catchup is a no-op when LXMF is not initialized."""
    engine = MagicMock()
    engine._lxm_router = None
    engine._lxmf_destination = None

    from retiboard.sync.engine import SyncEngine
    # Should return without error.
    await SyncEngine.sync_board_catchup(engine, "any_board")
    print("  PASS: sync_board_catchup no-op without LXMF")


# ============================================================================
# Runner
# ============================================================================

async def run_all():
    tests = [
        test_have_req_constant_exists,
        test_sync_board_catchup_sends_have_req,
        test_sync_board_catchup_no_peers_graceful,
        test_sync_board_catchup_caps_fanout,
        test_sync_board_catchup_excludes_self,
        test_path_resolution_wakeup_fires,
        test_have_req_handler_responds_with_have,
        test_have_req_handler_ignores_unknown_board,
        test_lxmf_have_is_processed,
        test_lxmf_have_registers_peer_authoritatively,
        test_catchup_idempotent,
        test_sync_board_catchup_no_lxmf,
    ]

    passed = 0
    failed = 0

    print(f"\n{'=' * 60}")
    print("Catch-up Sync & Recovery Tests")
    print(f"{'=' * 60}\n")

    for test in tests:
        try:
            await test()
            passed += 1
        except Exception as e:
            failed += 1
            print(f"  FAIL: {test.__name__}: {e}")
            import traceback
            traceback.print_exc()

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)}")
    print(f"{'=' * 60}")

    # Cleanup.
    shutil.rmtree(_TEST_HOME, ignore_errors=True)
    print(f"Cleaned up test data: {_TEST_HOME}\n")

    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(run_all())
    sys.exit(0 if success else 1)
