# ruff: noqa: E402

import time

import pytest

aiosqlite = pytest.importorskip("aiosqlite")

from retiboard.chunks.models import ChunkPeerPenaltyRecord, ChunkRequestStateRecord
from retiboard.db.batcher import ChunkStateBatcher


def test_batcher_should_flush_on_pending_count():
    batcher = ChunkStateBatcher(board_id="b", max_pending=2, max_interval_seconds=60.0)
    batcher.queue_chunk_state(ChunkRequestStateRecord(session_id="s", chunk_index=0, state="requested"))
    assert batcher.should_flush() is False
    batcher.queue_chunk_state(ChunkRequestStateRecord(session_id="s", chunk_index=1, state="requested"))
    assert batcher.should_flush() is True


def test_batcher_should_flush_on_interval():
    batcher = ChunkStateBatcher(board_id="b", max_pending=10, max_interval_seconds=0.01)
    batcher.queue_peer_penalty(ChunkPeerPenaltyRecord(board_id="b", peer_lxmf_hash="p"))
    time.sleep(0.02)
    assert batcher.should_flush() is True


@pytest.mark.asyncio
async def test_batcher_requeues_on_flush_error(monkeypatch):
    batcher = ChunkStateBatcher(board_id="b")
    batcher.queue_chunk_state(ChunkRequestStateRecord(session_id="s", chunk_index=0, state="requested"))
    batcher.queue_peer_penalty(ChunkPeerPenaltyRecord(board_id="b", peer_lxmf_hash="p"))

    async def fail_get_board_connection(board_id: str):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr("retiboard.db.batcher.get_board_connection", fail_get_board_connection)

    with pytest.raises(RuntimeError):
        await batcher.flush()

    assert batcher.pending_count() == 2
