# ruff: noqa: E402

"""Pause regression tests for late-arriving chunk resources."""

from __future__ import annotations

import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if "RNS" not in sys.modules:
    sys.modules["RNS"] = types.SimpleNamespace(
        LOG_DEBUG=0,
        LOG_INFO=1,
        LOG_WARNING=2,
        LOG_ERROR=3,
        log=lambda *args, **kwargs: None,
        prettyhexrep=lambda value: str(value),
    )

from retiboard.chunks.models import ChunkManifest
from retiboard.chunks.swarm import ChunkFetchState, PriorityMode, SwarmFetcher
from retiboard.sync.payload_fetch import (
    _handle_incoming_chunk_resource,
    register_pending_chunk_session,
)


class _FakeReassembly:
    def __init__(self) -> None:
        self.writes: list[tuple[int, int, bytes]] = []

    def write_verified_chunk(self, chunk_index: int, offset: int, data: bytes) -> None:
        self.writes.append((chunk_index, offset, data))

    def is_complete(self) -> bool:
        return False


def test_paused_session_rejects_late_chunk_resource() -> None:
    loop = asyncio.new_event_loop()
    try:
        session = register_pending_chunk_session(
            "board-pause",
            "cd" * 32,
            "attachments",
            "peer-a",
            loop,
            session_id="sess-pause-late",
        )
        with session.lock:
            session.manifest = ChunkManifest(
                manifest_version=1,
                board_id="board-pause",
                post_id="p1",
                thread_id="t1",
                blob_kind="attachments",
                blob_hash="cd" * 32,
                blob_size=128,
                chunk_size=128,
                chunk_count=1,
            )
            session.reassembly = _FakeReassembly()
            session.validator = types.SimpleNamespace(
                prevalidate=lambda **_kwargs: types.SimpleNamespace(offset=0)
            )
            session.swarm = SwarmFetcher(
                peer_lxmf_hashes=["peer-a"],
                chunk_count=1,
                next_chunk_timeout=lambda _idx: 30.0,
                priority_mode=PriorityMode.HYBRID,
            )
            plan = session.swarm.plan_requests()[0]
            session.swarm.mark_request_sent(plan.request_id)
            session.paused = True

        _handle_incoming_chunk_resource(
            {
                "blob_hash": "cd" * 32,
                "board_id": "board-pause",
                "request_id": plan.request_id,
                "chunk_index": 0,
                "peer_lxmf_hash": "peer-a",
            },
            b"late-bytes",
        )

        with session.lock:
            assert session.stored_chunks == set()
            assert session.cancelled_request_ids == {plan.request_id}
            assert session.swarm.active_request_count() == 0
            assert session.swarm.lookup_request(plan.request_id) is None
            assert session.swarm.chunks[0].state == ChunkFetchState.MISSING
            assert session.swarm.chunks[0].stored is False
            assert session.swarm.peers["peer-a"].in_flight == 0
            assert session.reassembly.writes == []
    finally:
        loop.close()
