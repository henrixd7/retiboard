# ruff: noqa: E402

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

from retiboard.chunks.models import ChunkManifest, ChunkManifestEntry, ChunkRequestStateRecord
from retiboard.sync.message_queue import SendResult
from retiboard.sync.peers import PathState, PeerInfo
from retiboard.sync import payload_fetch


def test_warm_resume_reuses_live_session_state_without_manifest_rerequest() -> None:
    loop = asyncio.new_event_loop()
    session = payload_fetch.register_pending_chunk_session(
        "warm-board",
        "ab" * 32,
        "attachments",
        "peer-a",
        loop,
        session_id="sess-warm-resume",
    )
    session.manifest = ChunkManifest(
        manifest_version=1,
        board_id="warm-board",
        post_id="p1",
        thread_id="t1",
        blob_kind="attachments",
        blob_hash="ab" * 32,
        blob_size=1024,
        chunk_size=256,
        chunk_count=4,
    )
    session.entries = [
        ChunkManifestEntry(blob_hash="ab" * 32, chunk_index=0, offset=0, size=256, chunk_hash="h0"),
        ChunkManifestEntry(blob_hash="ab" * 32, chunk_index=1, offset=256, size=256, chunk_hash="h1"),
        ChunkManifestEntry(blob_hash="ab" * 32, chunk_index=2, offset=512, size=256, chunk_hash="h2"),
        ChunkManifestEntry(blob_hash="ab" * 32, chunk_index=3, offset=768, size=256, chunk_hash="h3"),
    ]
    session.entries_by_index = {entry.chunk_index: entry for entry in session.entries}
    session.reassembly = object()
    session.stored_chunks.update({0, 1})

    class _Peer:
        def __init__(self, lxmf_hash):
            self.lxmf_hash = lxmf_hash
            self.identity = object()

    class _PeerTracker:
        @staticmethod
        def get_fetch_peers(_board_id):
            return [_Peer("peer-a"), _Peer("peer-b")]

    persisted = types.SimpleNamespace(session_id="sess-warm-resume", blob_kind="attachments")
    prior_states = [
        ChunkRequestStateRecord(
            session_id="sess-warm-resume",
            chunk_index=0,
            state="stored",
            attempt_count=1,
        ),
        ChunkRequestStateRecord(
            session_id="sess-warm-resume",
            chunk_index=1,
            state="stored",
            attempt_count=2,
        ),
        ChunkRequestStateRecord(
            session_id="sess-warm-resume",
            chunk_index=2,
            state="missing",
            attempt_count=3,
        ),
    ]

    async def _fake_get_db(*_args, **_kwargs):
        return object()

    async def _fake_load_latest(*_args, **_kwargs):
        return persisted

    async def _fake_get_blob_ref(*_args, **_kwargs):
        return {"blob_kind": "attachments"}

    async def _fake_restore(*_args, **_kwargs):
        return False

    async def _fake_request_manifest(*_args, **_kwargs):
        raise AssertionError("warm resume should not request manifest again")

    async def _fake_load_availability(*_args, **_kwargs):
        return {}

    async def _fake_load_penalties(*_args, **_kwargs):
        return {}

    async def _fake_load_states(*_args, **_kwargs):
        return prior_states

    async def _fake_fetch_swarm(active_session, _sync_engine):
        assert active_session.swarm is not None
        assert active_session.swarm.chunks[0].stored is True
        assert active_session.swarm.chunks[1].stored is True
        assert active_session.swarm.chunks[2].stored is False
        assert active_session.swarm.chunks[2].attempt_count == 3
        return payload_fetch._CHUNK_FETCH_RESULT_PAUSED

    original_announce = payload_fetch.announce_payload_destination
    original_payload_exists = payload_fetch.payload_exists
    original_get_db = payload_fetch.get_board_connection
    original_load_latest = payload_fetch.load_latest_chunk_fetch_session_for_blob
    original_get_blob_ref = payload_fetch.get_blob_reference
    original_restore = payload_fetch._restore_chunk_session_state
    original_request_manifest = payload_fetch._request_chunk_manifest
    original_load_availability = payload_fetch.load_peer_chunk_availability
    original_load_penalties = payload_fetch.load_chunk_peer_penalties
    original_load_states = payload_fetch.load_chunk_request_states
    original_fetch_swarm = payload_fetch._fetch_manifest_chunks_swarm
    try:
        payload_fetch.announce_payload_destination = lambda: None
        payload_fetch.payload_exists = lambda *_args, **_kwargs: False
        payload_fetch.get_board_connection = _fake_get_db
        payload_fetch.load_latest_chunk_fetch_session_for_blob = _fake_load_latest
        payload_fetch.get_blob_reference = _fake_get_blob_ref
        payload_fetch._restore_chunk_session_state = _fake_restore
        payload_fetch._request_chunk_manifest = _fake_request_manifest
        payload_fetch.load_peer_chunk_availability = _fake_load_availability
        payload_fetch.load_chunk_peer_penalties = _fake_load_penalties
        payload_fetch.load_chunk_request_states = _fake_load_states
        payload_fetch._fetch_manifest_chunks_swarm = _fake_fetch_swarm

        result = asyncio.run(
            payload_fetch.fetch_payload_from_peers_chunked(
                "warm-board",
                "ab" * 32,
                _PeerTracker(),
                sync_engine=object(),
                expected_size=1024,
            )
        )
    finally:
        payload_fetch.announce_payload_destination = original_announce
        payload_fetch.payload_exists = original_payload_exists
        payload_fetch.get_board_connection = original_get_db
        payload_fetch.load_latest_chunk_fetch_session_for_blob = original_load_latest
        payload_fetch.get_blob_reference = original_get_blob_ref
        payload_fetch._restore_chunk_session_state = original_restore
        payload_fetch._request_chunk_manifest = original_request_manifest
        payload_fetch.load_peer_chunk_availability = original_load_availability
        payload_fetch.load_chunk_peer_penalties = original_load_penalties
        payload_fetch.load_chunk_request_states = original_load_states
        payload_fetch._fetch_manifest_chunks_swarm = original_fetch_swarm
        asyncio.run(payload_fetch.cancel_pending_chunk_session("ab" * 32))
        loop.close()

    assert result is payload_fetch._CHUNK_FETCH_RESULT_PAUSED


def test_warm_resume_prefers_peers_with_known_chunk_availability() -> None:
    loop = asyncio.new_event_loop()
    session = payload_fetch.register_pending_chunk_session(
        "warm-board-filtered",
        "cd" * 32,
        "attachments",
        "peer-a",
        loop,
        session_id="sess-warm-filtered",
    )
    session.manifest = ChunkManifest(
        manifest_version=1,
        board_id="warm-board-filtered",
        post_id="p1",
        thread_id="t1",
        blob_kind="attachments",
        blob_hash="cd" * 32,
        blob_size=1024,
        chunk_size=256,
        chunk_count=4,
    )
    session.entries = [
        ChunkManifestEntry(blob_hash="cd" * 32, chunk_index=0, offset=0, size=256, chunk_hash="h0"),
        ChunkManifestEntry(blob_hash="cd" * 32, chunk_index=1, offset=256, size=256, chunk_hash="h1"),
        ChunkManifestEntry(blob_hash="cd" * 32, chunk_index=2, offset=512, size=256, chunk_hash="h2"),
        ChunkManifestEntry(blob_hash="cd" * 32, chunk_index=3, offset=768, size=256, chunk_hash="h3"),
    ]
    session.entries_by_index = {entry.chunk_index: entry for entry in session.entries}
    session.reassembly = object()
    session.stored_chunks.update({0, 1})

    class _Peer:
        def __init__(self, lxmf_hash):
            self.lxmf_hash = lxmf_hash
            self.identity = object()

    class _PeerTracker:
        @staticmethod
        def get_fetch_peers(_board_id):
            return [_Peer("peer-a"), _Peer("peer-b")]

    persisted = types.SimpleNamespace(session_id="sess-warm-filtered", blob_kind="attachments")
    prior_states = [
        ChunkRequestStateRecord(
            session_id="sess-warm-filtered",
            chunk_index=0,
            state="stored",
            attempt_count=1,
        ),
        ChunkRequestStateRecord(
            session_id="sess-warm-filtered",
            chunk_index=1,
            state="stored",
            attempt_count=1,
        ),
    ]

    async def _fake_get_db(*_args, **_kwargs):
        return object()

    async def _fake_load_latest(*_args, **_kwargs):
        return persisted

    async def _fake_get_blob_ref(*_args, **_kwargs):
        return {"blob_kind": "attachments"}

    async def _fake_restore(*_args, **_kwargs):
        return False

    async def _fake_request_manifest(*_args, **_kwargs):
        raise AssertionError("warm resume should not request manifest again")

    async def _fake_load_availability(*_args, **_kwargs):
        return {
            "peer-a": {"complete": True, "ranges": [(0, 3)]},
            "peer-b": {"complete": False, "ranges": []},
        }

    async def _fake_load_penalties(*_args, **_kwargs):
        return {}

    async def _fake_load_states(*_args, **_kwargs):
        return prior_states

    async def _fake_fetch_swarm(active_session, _sync_engine):
        assert active_session.swarm is not None
        assert sorted(active_session.swarm.peers.keys()) == ["peer-a"]
        assert active_session.swarm.progress_snapshot()["peer_count"] == 1
        return payload_fetch._CHUNK_FETCH_RESULT_PAUSED

    original_announce = payload_fetch.announce_payload_destination
    original_payload_exists = payload_fetch.payload_exists
    original_get_db = payload_fetch.get_board_connection
    original_load_latest = payload_fetch.load_latest_chunk_fetch_session_for_blob
    original_get_blob_ref = payload_fetch.get_blob_reference
    original_restore = payload_fetch._restore_chunk_session_state
    original_request_manifest = payload_fetch._request_chunk_manifest
    original_load_availability = payload_fetch.load_peer_chunk_availability
    original_load_penalties = payload_fetch.load_chunk_peer_penalties
    original_load_states = payload_fetch.load_chunk_request_states
    original_fetch_swarm = payload_fetch._fetch_manifest_chunks_swarm
    try:
        payload_fetch.announce_payload_destination = lambda: None
        payload_fetch.payload_exists = lambda *_args, **_kwargs: False
        payload_fetch.get_board_connection = _fake_get_db
        payload_fetch.load_latest_chunk_fetch_session_for_blob = _fake_load_latest
        payload_fetch.get_blob_reference = _fake_get_blob_ref
        payload_fetch._restore_chunk_session_state = _fake_restore
        payload_fetch._request_chunk_manifest = _fake_request_manifest
        payload_fetch.load_peer_chunk_availability = _fake_load_availability
        payload_fetch.load_chunk_peer_penalties = _fake_load_penalties
        payload_fetch.load_chunk_request_states = _fake_load_states
        payload_fetch._fetch_manifest_chunks_swarm = _fake_fetch_swarm

        result = asyncio.run(
            payload_fetch.fetch_payload_from_peers_chunked(
                "warm-board-filtered",
                "cd" * 32,
                _PeerTracker(),
                sync_engine=object(),
                expected_size=1024,
            )
        )
    finally:
        payload_fetch.announce_payload_destination = original_announce
        payload_fetch.payload_exists = original_payload_exists
        payload_fetch.get_board_connection = original_get_db
        payload_fetch.load_latest_chunk_fetch_session_for_blob = original_load_latest
        payload_fetch.get_blob_reference = original_get_blob_ref
        payload_fetch._restore_chunk_session_state = original_restore
        payload_fetch._request_chunk_manifest = original_request_manifest
        payload_fetch.load_peer_chunk_availability = original_load_availability
        payload_fetch.load_chunk_peer_penalties = original_load_penalties
        payload_fetch.load_chunk_request_states = original_load_states
        payload_fetch._fetch_manifest_chunks_swarm = original_fetch_swarm
        asyncio.run(payload_fetch.cancel_pending_chunk_session("cd" * 32))
        loop.close()

    assert result is payload_fetch._CHUNK_FETCH_RESULT_PAUSED


def test_immediate_chunk_send_requests_path_without_queueing_chunk_request() -> None:
    requested: list[str] = []
    tried: list[tuple[str, str]] = []

    class _PeerTracker:
        def __init__(self) -> None:
            self.peer = PeerInfo(
                lxmf_hash="a" * 32,
                identity=object(),
                path_state=PathState.STALE,
            )

        def get_peer(self, peer_lxmf_hash: str):
            if peer_lxmf_hash == self.peer.lxmf_hash:
                return self.peer
            return None

        def record_delivery_failure(self, peer_lxmf_hash: str) -> None:
            requested.append(f"fail:{peer_lxmf_hash}")

        def mark_path_known(self, peer_lxmf_hash: str) -> None:
            requested.append(f"known:{peer_lxmf_hash}")

    sync_engine = types.SimpleNamespace(
        peer_tracker=_PeerTracker(),
        _request_path=lambda peer_lxmf_hash: requested.append(peer_lxmf_hash),
        _try_send_lxmf=lambda peer, payload, title: tried.append((peer.lxmf_hash, title)) or True,
    )

    result = payload_fetch._send_chunk_request_immediate(
        sync_engine,
        "a" * 32,
        b'{"chunk":1}',
    )

    assert result == SendResult.QUEUED
    assert requested == ["a" * 32]
    assert tried == []


def test_swarm_loop_does_not_hold_active_slot_for_deferred_chunk_request() -> None:
    loop = asyncio.new_event_loop()
    session = payload_fetch.PendingChunkSession(
        session_id="sess-deferred",
        board_id="board-deferred",
        blob_hash="ef" * 32,
        blob_kind="attachments",
        assigned_peer_lxmf_hash="peer-a",
        event_loop=loop,
    )
    session.manifest = ChunkManifest(
        manifest_version=1,
        board_id="board-deferred",
        post_id="p1",
        thread_id="t1",
        blob_kind="attachments",
        blob_hash="ef" * 32,
        blob_size=1024,
        chunk_size=256,
        chunk_count=1,
    )
    session.entries = [
        ChunkManifestEntry(blob_hash="ef" * 32, chunk_index=0, offset=0, size=256, chunk_hash="h0"),
    ]
    session.entries_by_index = {0: session.entries[0]}
    session.reassembly = object()
    session.swarm = payload_fetch.SwarmFetcher(
        peer_lxmf_hashes=["peer-a"],
        chunk_count=1,
        next_chunk_timeout=lambda _: 30.0,
        max_inflight_total=1,
        max_inflight_per_peer=1,
    )
    payload_fetch.get_payload_scheduler().register_session(
        session_id=session.session_id,
        blob_hash=session.blob_hash,
        blob_kind=session.blob_kind,
        expected_size=1024,
        manual_override=False,
    )

    async def _fake_wait_any(_events, _timeout):
        with session.lock:
            session.paused = True
        return None

    original_payload_exists = payload_fetch.payload_exists
    original_wait_any = payload_fetch._wait_any
    original_send_immediate = payload_fetch._send_chunk_request_immediate
    try:
        payload_fetch.payload_exists = lambda *_args, **_kwargs: False
        payload_fetch._wait_any = _fake_wait_any
        payload_fetch._send_chunk_request_immediate = lambda *_args, **_kwargs: SendResult.QUEUED

        result = asyncio.run(payload_fetch._fetch_manifest_chunks_swarm(session, types.SimpleNamespace()))
    finally:
        payload_fetch.payload_exists = original_payload_exists
        payload_fetch._wait_any = original_wait_any
        payload_fetch._send_chunk_request_immediate = original_send_immediate
        payload_fetch.get_payload_scheduler().unregister_session(session.session_id)
        loop.close()

    assert result is payload_fetch._CHUNK_FETCH_RESULT_PAUSED
    assert session.swarm is not None
    assert session.swarm.active_request_count() == 0
    assert session.swarm.chunks[0].attempt_count == 0
    assert session.swarm.chunks[0].state == payload_fetch.ChunkFetchState.REQUEST_ENQUEUED
