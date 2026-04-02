# ruff: noqa: E402

from __future__ import annotations

import pytest
pytest.importorskip("aiosqlite")

import asyncio
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TEST_HOME = tempfile.mkdtemp(prefix="retiboard_phase5_")
os.environ["RETIBOARD_HOME"] = _TEST_HOME

import retiboard.config as config_module
import retiboard.db.database as database_module
import retiboard.db.pool as pool_module
from retiboard.chunks.models import ChunkManifest
from retiboard.chunks.swarm import PriorityMode, SwarmFetcher
from retiboard.db.database import (
    load_chunk_peer_penalties,
    open_board_db,
    save_board_config,
    upsert_chunk_peer_penalty,
)
from retiboard.db.models import Board
from retiboard.sync.payload_fetch import (
    get_chunk_fetch_progress,
    register_pending_chunk_session,
)


def _repoint_retiboard_home() -> None:
    home = Path(_TEST_HOME)
    config_module.RETIBOARD_HOME = home
    config_module.BOARDS_DIR = home / "boards"
    database_module.BOARDS_DIR = config_module.BOARDS_DIR


async def _reset_board_pool() -> None:
    if pool_module._pool is not None:
        await pool_module._pool.close_all()
    pool_module._pool = None


def make_board(board_id: str = "phase5board") -> Board:
    return Board(
        board_id=board_id,
        display_name="Phase 5 Board",
        text_only=False,
        default_ttl_seconds=43200,
        bump_decay_rate=3600,
        pow_difficulty=0,
        announce_version=2,
        peer_lxmf_hash="",
    )


def test_swarm_applies_persisted_cooldown_state() -> None:
    swarm = SwarmFetcher(
        peer_lxmf_hashes=["peer-a", "peer-b"],
        chunk_count=4,
        next_chunk_timeout=lambda _idx: 30.0,
        priority_mode=PriorityMode.HYBRID,
    )
    swarm.apply_persisted_peer_state(
        "peer-a",
        timeout_count=2,
        invalid_chunk_count=1,
        success_count=3,
        cooldown_until=time.time() + 90.0,
    )

    peer = swarm.get_peer_state("peer-a")
    assert peer is not None
    assert peer.timeout_count == 2
    assert peer.invalid_chunk_count == 1
    assert peer.success_count == 3
    assert peer.is_available(time.time()) is False
    assert swarm.active_peer_count() == 1



def test_live_progress_snapshot_reports_resumed_partial_session() -> None:
    loop = asyncio.new_event_loop()
    try:
        session = register_pending_chunk_session(
            "progress_board",
            "ab" * 32,
            "attachments",
            "peer-a",
            loop,
            session_id="sess-progress",
        )
        session.resumed_from_persisted = True
        session.manifest = ChunkManifest(
            manifest_version=1,
            board_id="progress_board",
            post_id="p1",
            thread_id="t1",
            blob_kind="attachments",
            blob_hash="ab" * 32,
            blob_size=1024,
            chunk_size=256,
            chunk_count=4,
        )
        session.stored_chunks.update({0, 1})
        session.candidate_peer_lxmf_hashes = ["peer-a", "peer-b"]
        session.swarm = SwarmFetcher(
            peer_lxmf_hashes=["peer-a", "peer-b"],
            chunk_count=4,
            next_chunk_timeout=lambda _idx: 30.0,
            priority_mode=PriorityMode.HYBRID,
        )
        session.swarm.chunks[0].stored = True
        session.swarm.chunks[1].stored = True
        progress = asyncio.run(get_chunk_fetch_progress("progress_board", "ab" * 32))
        assert progress is not None
        assert progress["resumed_from_persisted"] is True
        assert progress["stored_chunks"] == 2
        assert progress["chunk_count"] == 4
        assert progress["percent_complete"] == 50
        assert progress["state"] == "fetching"
    finally:
        loop.close()



def test_chunk_peer_penalty_roundtrip() -> None:
    async def runner() -> None:
        _repoint_retiboard_home()
        await _reset_board_pool()
        board = make_board("phase5_penalty_db")
        db = await open_board_db(board.board_id)
        try:
            await save_board_config(db, board)
            from retiboard.chunks.models import ChunkPeerPenaltyRecord

            await upsert_chunk_peer_penalty(
                db,
                ChunkPeerPenaltyRecord(
                    board_id=board.board_id,
                    peer_lxmf_hash="peer-z",
                    timeout_count=4,
                    invalid_chunk_count=2,
                    success_count=7,
                    cooldown_until=int(time.time()) + 120,
                    updated_at=int(time.time()),
                ),
            )
            rows = await load_chunk_peer_penalties(
                db,
                board_id=board.board_id,
                peer_lxmf_hashes=["peer-z"],
            )
        finally:
            await db.close()

        assert "peer-z" in rows
        row = rows["peer-z"]
        assert row.timeout_count == 4
        assert row.invalid_chunk_count == 2
        assert row.success_count == 7
        assert row.cooldown_until > int(time.time())

    asyncio.run(runner())



def teardown_module(module) -> None:
    shutil.rmtree(_TEST_HOME, ignore_errors=True)
