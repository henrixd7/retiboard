# ruff: noqa: E402

"""
Phase 1 Verification Tests — Local Storage Layer.

Run with: python -m pytest tests/test_phase1.py -v
    or:   python tests/test_phase1.py  (standalone, no pytest needed)

Tests cover:
    1. Board creation + config persistence
    2. Post insertion (OP + replies) with correct schema fields
    3. Thread bumping (thread_last_activity updates)
    4. Catalog view (sorted by bump order)
    5. Pruning: expired threads (thread TTL expiry)
    6. Pruning: thread-level retention (no per-post expiry)
    7. Thread cap enforcement
    8. Opaque payload storage (write, read, verify, delete)
    9. Payload hash verification (reject tampered data)
   10. Per-board isolation (separate databases + payload dirs)
   11. Zero-content invariant (no content columns in DB)
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import tempfile
import time
import sys

# Ensure the project root is on the path.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Override RETIBOARD_HOME before importing anything else.
_TEST_HOME = tempfile.mkdtemp(prefix="retiboard_test_")
os.environ["RETIBOARD_HOME"] = _TEST_HOME

# Now import — config.py will pick up the env var.
from retiboard.db.models import Board, PostMetadata
from retiboard.db.database import (
    open_board_db,
    board_db_path,
    board_payloads_dir,
    save_board_config,
    load_board_config,
    insert_post,
    get_post,
    get_catalog,
    get_thread_count,
    mark_expired_threads,
    delete_abandoned_threads,
    enforce_thread_cap,
)
from retiboard.pruning.pruner import prune_board
from retiboard.storage.payloads import (
    write_payload,
    read_payload,
    delete_payload,
    delete_payloads_bulk,
    payload_exists,
)


# =============================================================================
# Helpers
# =============================================================================

def make_board(board_id: str = "testboard01", **overrides) -> Board:
    """Create a test board with sensible defaults."""
    defaults = dict(
        board_id=board_id,
        display_name="Test Board",
        text_only=False,
        default_ttl_seconds=43_200,   # 12h thread start TTL
        bump_decay_rate=3_600,        # 1h per-bump refill
        pow_difficulty=0,
        key_material="deadbeef" * 4,
        announce_version=1,
    )
    defaults.update(overrides)
    return Board(**defaults)


def make_post(
    post_id: str,
    thread_id: str,
    parent_id: str = "",
    timestamp: int = 0,
    ttl: int = 43_200,
    bump_flag: bool = True,
    content: bytes = b"opaque encrypted blob",
    has_attachments: bool = False,
    text_only: bool = False,
) -> tuple[PostMetadata, bytes]:
    """Create a test post + its fake encrypted payload."""
    if timestamp == 0:
        timestamp = int(time.time())
    content_hash = hashlib.sha256(content).hexdigest()
    post = PostMetadata(
        post_id=post_id,
        thread_id=thread_id,
        parent_id=parent_id,
        timestamp=timestamp,
        expiry_timestamp=timestamp + ttl,
        bump_flag=bump_flag,
        content_hash=content_hash,
        payload_size=len(content),
        has_attachments=has_attachments,
        text_only=text_only,
        identity_hash="",
        pow_nonce="0000",
        public_key="",
        encrypted_pings=[],
        edit_signature="",
        thread_last_activity=timestamp if (post_id == thread_id) else 0,
        is_abandoned=False,
    )
    return post, content


# =============================================================================
# Tests
# =============================================================================

async def test_01_board_creation():
    """Test: board config saved and loaded correctly."""
    print("  [01] Board creation + config persistence...")
    board = make_board()
    db = await open_board_db(board.board_id)
    try:
        await save_board_config(db, board)
        loaded = await load_board_config(db)
        assert loaded is not None, "Board config should exist after save"
        assert loaded.board_id == board.board_id
        assert loaded.display_name == "Test Board"
        assert loaded.default_ttl_seconds == 43_200
        assert loaded.bump_decay_rate == 3_600
        assert loaded.key_material == ""  # §5: key_material never in DB
        assert loaded.pow_difficulty == 0
        assert loaded.text_only is False
        print("    PASS")
    finally:
        await db.close()


async def test_02_post_insertion():
    """Test: OP + reply insertion with correct fields."""
    print("  [02] Post insertion (OP + reply)...")
    board = make_board()
    db = await open_board_db(board.board_id)
    try:
        await save_board_config(db, board)

        # Insert OP
        now = int(time.time())
        op, op_data = make_post("op001", "op001", timestamp=now)
        await insert_post(db, op)

        # Insert reply
        reply, reply_data = make_post(
            "reply001", "op001", parent_id="op001",
            timestamp=now + 60, bump_flag=True,
        )
        await insert_post(db, reply)

        # Verify OP
        fetched_op = await get_post(db, "op001")
        assert fetched_op is not None
        assert fetched_op.is_op is True
        assert fetched_op.thread_id == "op001"
        assert fetched_op.expiry_timestamp == now + 43_260

        # Verify reply
        fetched_reply = await get_post(db, "reply001")
        assert fetched_reply is not None
        assert fetched_reply.is_op is False
        assert fetched_reply.parent_id == "op001"

        print("    PASS")
    finally:
        await db.close()


async def test_03_thread_bumping():
    """Test: bumping reply updates OP activity and refills capped thread TTL."""
    print("  [03] Thread bumping (thread_last_activity update)...")
    board = make_board()
    db = await open_board_db(board.board_id)
    try:
        await save_board_config(db, board)

        now = 1_000_000
        op, _ = make_post("bump_op", "bump_op", timestamp=now)
        await insert_post(db, op)

        # Verify initial thread_last_activity
        fetched = await get_post(db, "bump_op")
        assert fetched.thread_last_activity == now

        # Bumping reply at now+600
        reply, _ = make_post(
            "bump_r1", "bump_op", parent_id="bump_op",
            timestamp=now + 600, bump_flag=True,
        )
        await insert_post(db, reply)

        # OP's thread_last_activity should be updated and TTL refilled to cap.
        fetched = await get_post(db, "bump_op")
        assert fetched.thread_last_activity == now + 600, \
            f"Expected {now + 600}, got {fetched.thread_last_activity}"
        assert fetched.expiry_timestamp == now + 43_800
        fetched_reply = await get_post(db, "bump_r1")
        assert fetched_reply is not None
        assert fetched_reply.expiry_timestamp == now + 43_800

        # Sage reply (bump_flag=False) should NOT update activity or TTL.
        sage, _ = make_post(
            "bump_r2", "bump_op", parent_id="bump_op",
            timestamp=now + 1200, bump_flag=False,
        )
        await insert_post(db, sage)
        fetched = await get_post(db, "bump_op")
        assert fetched.thread_last_activity == now + 600, \
            "Sage reply should not bump thread"
        assert fetched.expiry_timestamp == now + 43_800

        print("    PASS")
    finally:
        await db.close()


async def test_04_catalog_view():
    """Test: catalog returns threads sorted by bump order."""
    print("  [04] Catalog view (bump order)...")
    board = make_board(board_id="catalog_board")
    db = await open_board_db(board.board_id)
    try:
        await save_board_config(db, board)

        # Create 3 threads with different activity times.
        for i, ts in enumerate([1000, 3000, 2000]):
            op, _ = make_post(f"cat_op{i}", f"cat_op{i}", timestamp=ts)
            await insert_post(db, op)

        catalog = await get_catalog(db)
        assert len(catalog) == 3
        # Should be sorted: newest bump first → 3000, 2000, 1000
        assert catalog[0].thread_id == "cat_op1"  # ts=3000
        assert catalog[1].thread_id == "cat_op2"  # ts=2000
        assert catalog[2].thread_id == "cat_op0"  # ts=1000

        # Verify post counts
        assert all(t.post_count == 1 for t in catalog)

        print("    PASS")
    finally:
        await db.close()


async def test_05_expired_thread_pruning():
    """Test: threads past their thread TTL are marked expired and deleted."""
    print("  [05] Expired thread pruning...")
    board = make_board(board_id="prune_board", default_ttl_seconds=100, bump_decay_rate=10)
    db = await open_board_db(board.board_id)
    try:
        await save_board_config(db, board)

        now = 100_000

        # Thread A: started earlier, but a bump refilled its TTL so it is still active.
        op_a, _ = make_post("prune_a", "prune_a", timestamp=now - 100, ttl=100)
        await insert_post(db, op_a)
        reply_a, _ = make_post(
            "prune_a_r", "prune_a", parent_id="prune_a",
            timestamp=now - 5, ttl=100, bump_flag=True,
        )
        await insert_post(db, reply_a)

        op_a_check = await get_post(db, "prune_a")
        assert op_a_check is not None
        assert op_a_check.expiry_timestamp == now + 10, \
            f"Expected bumped thread expiry {now + 10}, got {op_a_check.expiry_timestamp}"

        # Thread B: expired (expires at now-100)
        op_b, _ = make_post("prune_b", "prune_b", timestamp=now - 200, ttl=100)
        await insert_post(db, op_b)
        # Add a reply to thread B (also expired with the thread)
        reply_b, _ = make_post(
            "prune_b_r", "prune_b", parent_id="prune_b",
            timestamp=now - 150, ttl=100, bump_flag=False,
        )
        await insert_post(db, reply_b)

        # Mark expired
        expired = await mark_expired_threads(db, now=now)
        assert "prune_b" in expired, f"Thread B should be expired, got {expired}"
        assert "prune_a" not in expired, "Thread A should NOT be expired"

        # Verify OP B is marked
        op_b_check = await get_post(db, "prune_b")
        assert op_b_check.is_abandoned is True

        # Delete abandoned → should remove thread B entirely (OP + reply)
        deleted = await delete_abandoned_threads(db)
        assert len(deleted) == 1
        assert deleted[0][0] == "prune_b"
        assert len(deleted[0][1]) == 2  # 2 content hashes (OP + reply)

        # Thread B posts should be gone
        assert await get_post(db, "prune_b") is None
        assert await get_post(db, "prune_b_r") is None

        # Thread A should still exist with its bumped reply.
        assert await get_post(db, "prune_a") is not None
        assert await get_post(db, "prune_a_r") is not None

        print("    PASS")
    finally:
        await db.close()


async def test_06_thread_level_retention():
    """Test: active threads keep all posts regardless of per-post expiry."""
    print("  [06] Thread-level retention...")
    board = make_board(board_id="expiry_board")
    now = 200_000
    db = await open_board_db(board.board_id)
    try:
        await save_board_config(db, board)

        op, _ = make_post("exp_op", "exp_op", timestamp=now - 60, ttl=43_200)
        await insert_post(db, op)

        reply, _ = make_post(
            "exp_r1", "exp_op", parent_id="exp_op",
            timestamp=now - 200, ttl=100,
        )
        await insert_post(db, reply)
    finally:
        await db.close()

    result = await prune_board(board.board_id, now=now)
    assert result.threads_deleted == 0

    db = await open_board_db(board.board_id)
    try:
        assert await get_post(db, "exp_r1") is not None
        assert await get_post(db, "exp_op") is not None
        print("    PASS")
    finally:
        await db.close()


async def test_06b_pinned_thread_skips_expiry_mark():
    """Test: pinned threads are exempt from expiry marking."""
    print("  [06b] Pinned thread expiry exemption...")
    board = make_board(board_id="pinned_expiry_board")
    now = 250_000
    db = await open_board_db(board.board_id)
    try:
        await save_board_config(db, board)

        op, _ = make_post("pin_exp", "pin_exp", timestamp=now - 200, ttl=100)
        await insert_post(db, op)

        expired = await mark_expired_threads(db, now=now, pinned_thread_ids={"pin_exp"})
        assert expired == []

        op_check = await get_post(db, "pin_exp")
        assert op_check is not None
        assert op_check.is_abandoned is False
        print("    PASS")
    finally:
        await db.close()


async def test_07_thread_cap():
    """Test: enforce_thread_cap removes oldest threads."""
    print("  [07] Thread cap enforcement...")
    board = make_board(board_id="cap_board", max_active_threads_local=3)
    db = await open_board_db(board.board_id)
    try:
        await save_board_config(db, board)

        # Create 5 threads
        for i in range(5):
            op, _ = make_post(f"cap_{i}", f"cap_{i}", timestamp=1000 + i * 100)
            await insert_post(db, op)

        assert await get_thread_count(db) == 5

        # Enforce cap of 3 → should remove 2 oldest
        deleted = await enforce_thread_cap(db, max_threads=3)
        assert len(deleted) == 2
        assert await get_thread_count(db) == 3

        # The 2 oldest (cap_0 ts=1000, cap_1 ts=1100) should be gone
        assert await get_post(db, "cap_0") is None
        assert await get_post(db, "cap_1") is None
        # The 3 newest should remain
        assert await get_post(db, "cap_2") is not None
        assert await get_post(db, "cap_3") is not None
        assert await get_post(db, "cap_4") is not None

        print("    PASS")
    finally:
        await db.close()


async def test_07b_thread_cap_skips_pinned_threads():
    """Test: pinned threads are exempt from thread-cap pruning."""
    print("  [07b] Pinned thread cap exemption...")
    board = make_board(board_id="cap_pinned_board", max_active_threads_local=2)
    db = await open_board_db(board.board_id)
    try:
        await save_board_config(db, board)

        for i in range(3):
            op, _ = make_post(f"cap_pin_{i}", f"cap_pin_{i}", timestamp=1000 + i * 100)
            await insert_post(db, op)

        deleted = await enforce_thread_cap(db, max_threads=2, pinned_thread_ids={"cap_pin_0"})
        assert len(deleted) == 1
        assert deleted[0][0] == "cap_pin_1"
        assert await get_post(db, "cap_pin_0") is not None
        assert await get_post(db, "cap_pin_1") is None
        assert await get_post(db, "cap_pin_2") is not None
        print("    PASS")
    finally:
        await db.close()


async def test_08_payload_storage():
    """Test: opaque payload write/read/delete."""
    print("  [08] Opaque payload storage...")
    board_id = "payload_board"
    # Ensure dir exists
    board_payloads_dir(board_id).mkdir(parents=True, exist_ok=True)

    data = b"this is fake encrypted AES-GCM blob with nonce prefix"
    content_hash = hashlib.sha256(data).hexdigest()

    # Write
    path = write_payload(board_id, content_hash, data)
    assert path.exists()
    assert path.name == f"{content_hash}.bin"

    # Read
    read_back = read_payload(board_id, content_hash)
    assert read_back == data

    # Exists check
    assert payload_exists(board_id, content_hash)

    # Delete
    assert delete_payload(board_id, content_hash) is True
    assert not payload_exists(board_id, content_hash)
    assert read_payload(board_id, content_hash) is None

    # Delete idempotent
    assert delete_payload(board_id, content_hash) is False

    print("    PASS")


async def test_09_payload_hash_verification():
    """Test: tampered payloads are rejected."""
    print("  [09] Payload hash verification (reject tampered)...")
    board_id = "verify_board"
    board_payloads_dir(board_id).mkdir(parents=True, exist_ok=True)

    data = b"legitimate encrypted content"
    content_hash = hashlib.sha256(data).hexdigest()

    # Write with correct hash → should work
    write_payload(board_id, content_hash, data, verify_hash=True)
    assert payload_exists(board_id, content_hash)
    delete_payload(board_id, content_hash)

    # Write with wrong hash → should raise ValueError
    try:
        write_payload(board_id, "badhash" * 4, data, verify_hash=True)
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "mismatch" in str(e).lower()

    print("    PASS")


async def test_10_per_board_isolation():
    """Test: two boards have completely separate databases and payloads."""
    print("  [10] Per-board isolation...")
    board_a = make_board(board_id="iso_board_a", display_name="Board A")
    board_b = make_board(board_id="iso_board_b", display_name="Board B")

    db_a = await open_board_db(board_a.board_id)
    db_b = await open_board_db(board_b.board_id)
    try:
        await save_board_config(db_a, board_a)
        await save_board_config(db_b, board_b)

        # Insert post in board A
        op_a, data_a = make_post("iso_op_a", "iso_op_a", timestamp=5000)
        await insert_post(db_a, op_a)
        write_payload(board_a.board_id, op_a.content_hash, data_a)

        # Board B should have no posts
        assert await get_post(db_b, "iso_op_a") is None
        assert not payload_exists(board_b.board_id, op_a.content_hash)

        # Insert different post in board B
        op_b, data_b = make_post(
            "iso_op_b", "iso_op_b", timestamp=6000,
            content=b"different content for board b",
        )
        await insert_post(db_b, op_b)
        write_payload(board_b.board_id, op_b.content_hash, data_b)

        # Cross-check: each board only sees its own data
        assert await get_thread_count(db_a) == 1
        assert await get_thread_count(db_b) == 1
        assert await get_post(db_a, "iso_op_b") is None
        assert await get_post(db_b, "iso_op_a") is None

        # Verify separate filesystem paths
        assert board_db_path("iso_board_a") != board_db_path("iso_board_b")

        print("    PASS")
    finally:
        await db_a.close()
        await db_b.close()


async def test_11_zero_content_invariant():
    """Test: database schema has NO content columns."""
    print("  [11] Zero-content invariant (schema check)...")
    board = make_board(board_id="schema_check")
    db = await open_board_db(board.board_id)
    try:
        # Get column names from the posts table.
        async with db.execute("PRAGMA table_info(posts)") as cur:
            rows = await cur.fetchall()
            column_names = {row[1] for row in rows}  # row[1] is column name

        # These columns are PROHIBITED by §3.1.
        prohibited = {
            "subject", "title", "text", "body", "content",
            "preview", "snippet", "summary", "filename",
            "file_name", "file_ext", "image_hint", "thumbnail",
            "media_type", "mime_type",
        }

        violations = column_names & prohibited
        assert not violations, \
            f"§3.1 VIOLATION: found content columns in posts table: {violations}"

        # Verify expected columns ARE present.
        expected = {
            "post_id", "thread_id", "parent_id", "timestamp",
            "expiry_timestamp", "bump_flag", "content_hash",
            "payload_size", "has_attachments", "text_only", "identity_hash",
            "pow_nonce", "thread_last_activity", "is_abandoned",
        }
        missing = expected - column_names
        assert not missing, f"Missing expected columns: {missing}"

        print("    PASS")
    finally:
        await db.close()


async def test_12_bulk_payload_delete():
    """Test: bulk payload deletion for thread pruning."""
    print("  [12] Bulk payload deletion...")
    board_id = "bulk_board"
    board_payloads_dir(board_id).mkdir(parents=True, exist_ok=True)

    hashes = []
    for i in range(5):
        data = f"blob_{i}".encode()
        ch = hashlib.sha256(data).hexdigest()
        write_payload(board_id, ch, data)
        hashes.append(ch)

    assert all(payload_exists(board_id, h) for h in hashes)

    deleted = delete_payloads_bulk(board_id, hashes)
    assert deleted == 5
    assert not any(payload_exists(board_id, h) for h in hashes)

    print("    PASS")


# =============================================================================
# Runner
# =============================================================================

async def run_all():
    """Run all tests sequentially."""
    print("\nRetiBoard Phase 1 — Verification Tests")
    print(f"Test data directory: {_TEST_HOME}\n")

    tests = [
        test_01_board_creation,
        test_02_post_insertion,
        test_03_thread_bumping,
        test_04_catalog_view,
        test_05_expired_thread_pruning,
        test_06_thread_level_retention,
        test_06b_pinned_thread_skips_expiry_mark,
        test_07_thread_cap,
        test_07b_thread_cap_skips_pinned_threads,
        test_08_payload_storage,
        test_09_payload_hash_verification,
        test_10_per_board_isolation,
        test_11_zero_content_invariant,
        test_12_bulk_payload_delete,
    ]

    passed = 0
    failed = 0
    for test_fn in tests:
        try:
            await test_fn()
            passed += 1
        except Exception as e:
            print(f"    FAIL: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n{'=' * 50}")
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)}")
    print(f"{'=' * 50}")

    # Cleanup test directory.
    shutil.rmtree(_TEST_HOME, ignore_errors=True)
    print(f"Cleaned up test data: {_TEST_HOME}\n")

    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(run_all())
    sys.exit(0 if success else 1)
