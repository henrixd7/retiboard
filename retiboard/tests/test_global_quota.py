import os
import shutil
import tempfile
import time
import pytest
from pathlib import Path

@pytest.mark.asyncio
async def test_global_quota_enforcement(monkeypatch):
    # Setup temp home before importing logic that uses it
    test_home = tempfile.mkdtemp(prefix="retiboard_quota_test_")
    os.environ["RETIBOARD_HOME"] = test_home
    
    # Deferred imports to ensure they pick up the environment
    from retiboard.db.database import open_board_db, save_board_config, insert_post
    from retiboard.db.models import Board, PostMetadata
    from retiboard.pruning.pruner import enforce_global_quota
    from retiboard.settings import get_settings, GlobalSettings
    import retiboard.config
    import retiboard.pruning.pruner
    import retiboard.db.database

    # Ensure all modules use the same test home
    monkeypatch.setattr(retiboard.config, "RETIBOARD_HOME", Path(test_home))
    monkeypatch.setattr(retiboard.config, "BOARDS_DIR", Path(test_home) / "boards")
    monkeypatch.setattr(retiboard.pruning.pruner, "BOARDS_DIR", Path(test_home) / "boards")
    monkeypatch.setattr(retiboard.db.database, "BOARDS_DIR", Path(test_home) / "boards")
    GlobalSettings._instance = None

    from retiboard.config import BOARDS_DIR
    BOARDS_DIR.mkdir(parents=True, exist_ok=True)

    try:
        # Set a small quota: 1 MB
        settings = get_settings()
        settings.set("global_storage_limit_mb", 1)

        # Create two boards
        boards = ["board1", "board2"]
        for bid in boards:
            payload_dir = BOARDS_DIR / bid / "payloads"
            payload_dir.mkdir(parents=True, exist_ok=True)
            db = await open_board_db(bid)
            await save_board_config(db, Board(
                board_id=bid, display_name=f"Board {bid}", text_only=0,
                default_ttl_seconds=86400, bump_decay_rate=3600,
                max_active_threads_local=50,
                pow_difficulty=0, announce_version=2,
                subscribed_at=int(time.time())
            ))
            
            # Create a "large" payload (600 KB each -> 1.2 MB total > 1MB quota)
            content_hash = f"hash_{bid}"
            payload_path = payload_dir / f"{content_hash}.bin"
            with open(payload_path, "wb") as f:
                f.write(os.urandom(600 * 1024))
            
            # Save OP post
            post = PostMetadata(
                post_id=f"op_{bid}", thread_id=f"op_{bid}", parent_id="",
                timestamp=int(time.time()) - (100 if bid == "board1" else 50), # board1 is older
                expiry_timestamp=int(time.time()) + 86400,
                bump_flag=1, content_hash=content_hash, payload_size=600*1024,
                has_attachments=0, text_only=0, identity_hash="id",
                pow_nonce="nonce", thread_last_activity=int(time.time()) - (100 if bid == "board1" else 50)
            )
            await insert_post(db, post)
            await db.close()

        # Run quota enforcement
        result = await enforce_global_quota()
        
        # Board 1 (older) should have been pruned
        assert result.threads_quota_pruned == 1
        assert result.payloads_deleted == 1
        
        # Verify board1 payload is gone, board2 remains
        assert not (BOARDS_DIR / "board1" / "payloads" / "hash_board1.bin").exists()
        assert (BOARDS_DIR / "board2" / "payloads" / "hash_board2.bin").exists()

    finally:
        shutil.rmtree(test_home, ignore_errors=True)


@pytest.mark.asyncio
async def test_global_quota_skips_pinned_threads(monkeypatch):
    test_home = tempfile.mkdtemp(prefix="retiboard_quota_pinned_test_")
    os.environ["RETIBOARD_HOME"] = test_home

    from retiboard.db.database import open_board_db, save_board_config, insert_post
    from retiboard.db.models import Board, PostMetadata
    from retiboard.pruning.pruner import enforce_global_quota
    from retiboard.settings import get_settings, GlobalSettings
    import retiboard.config
    import retiboard.pruning.pruner
    import retiboard.db.database

    monkeypatch.setattr(retiboard.config, "RETIBOARD_HOME", Path(test_home))
    monkeypatch.setattr(retiboard.config, "BOARDS_DIR", Path(test_home) / "boards")
    monkeypatch.setattr(retiboard.pruning.pruner, "BOARDS_DIR", Path(test_home) / "boards")
    monkeypatch.setattr(retiboard.db.database, "BOARDS_DIR", Path(test_home) / "boards")
    GlobalSettings._instance = None

    from retiboard.config import BOARDS_DIR
    BOARDS_DIR.mkdir(parents=True, exist_ok=True)

    try:
        settings = get_settings()
        settings.set("global_storage_limit_mb", 1)
        settings.set("pinned_threads", ["board1:op_board1"])

        boards = ["board1", "board2"]
        for bid in boards:
            payload_dir = BOARDS_DIR / bid / "payloads"
            payload_dir.mkdir(parents=True, exist_ok=True)
            db = await open_board_db(bid)
            await save_board_config(db, Board(
                board_id=bid, display_name=f"Board {bid}", text_only=0,
                default_ttl_seconds=86400, bump_decay_rate=3600,
                max_active_threads_local=50,
                pow_difficulty=0, announce_version=2,
                subscribed_at=int(time.time())
            ))

            content_hash = f"hash_{bid}"
            payload_path = payload_dir / f"{content_hash}.bin"
            with open(payload_path, "wb") as f:
                f.write(os.urandom(600 * 1024))

            post = PostMetadata(
                post_id=f"op_{bid}", thread_id=f"op_{bid}", parent_id="",
                timestamp=int(time.time()) - (100 if bid == "board1" else 50),
                expiry_timestamp=int(time.time()) + 86400,
                bump_flag=1, content_hash=content_hash, payload_size=600 * 1024,
                has_attachments=0, text_only=0, identity_hash="id",
                pow_nonce="nonce", thread_last_activity=int(time.time()) - (100 if bid == "board1" else 50)
            )
            await insert_post(db, post)
            await db.close()

        result = await enforce_global_quota()

        assert result.threads_quota_pruned == 1
        assert (BOARDS_DIR / "board1" / "payloads" / "hash_board1.bin").exists()
        assert not (BOARDS_DIR / "board2" / "payloads" / "hash_board2.bin").exists()
    finally:
        shutil.rmtree(test_home, ignore_errors=True)
