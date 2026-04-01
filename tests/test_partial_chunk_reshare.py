# ruff: noqa: E402

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
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
        Resource=types.SimpleNamespace(COMPLETE=1),
    )

_TEST_HOME = tempfile.mkdtemp(prefix="retiboard_partial_reshare_")
os.environ["RETIBOARD_HOME"] = _TEST_HOME

import retiboard.config as config_module
import retiboard.db.database as database_module
import retiboard.db.pool as pool_module
import retiboard.sync.payload_fetch as payload_fetch
from retiboard.chunks.models import ChunkManifest, ChunkManifestEntry
from retiboard.chunks.reassembly import ReassemblyBuffer
from retiboard.chunks.swarm import SwarmFetcher
from retiboard.storage.payloads import chunk_assembly_path


def _repoint_retiboard_home() -> None:
    home = Path(_TEST_HOME)
    config_module.RETIBOARD_HOME = home
    config_module.BOARDS_DIR = home / "boards"
    database_module.BOARDS_DIR = config_module.BOARDS_DIR


async def _reset_board_pool() -> None:
    if pool_module._pool is not None:
        await pool_module._pool.close_all()
    pool_module._pool = None


def _reset_pending_chunk_registry() -> None:
    with payload_fetch._pending_chunk_lock:
        payload_fetch._pending_chunk_sessions.clear()
        payload_fetch._pending_chunk_by_blob.clear()


def _build_manifest(board_id: str, blob_hash: str, chunk_datas: list[bytes]) -> tuple[ChunkManifest, list[ChunkManifestEntry]]:
    entries: list[ChunkManifestEntry] = []
    offset = 0
    for idx, chunk in enumerate(chunk_datas):
        entries.append(
            ChunkManifestEntry(
                blob_hash=blob_hash,
                chunk_index=idx,
                offset=offset,
                size=len(chunk),
                chunk_hash=hashlib.sha256(chunk).hexdigest(),
            )
        )
        offset += len(chunk)
    manifest = ChunkManifest(
        manifest_version=1,
        board_id=board_id,
        post_id="p1",
        thread_id="t1",
        blob_kind="text",
        blob_hash=blob_hash,
        blob_size=offset,
        chunk_size=len(chunk_datas[0]),
        chunk_count=len(chunk_datas),
    )
    return manifest, entries


class _FakeDecision:
    def __init__(self, allowed: bool, reason: str | None = None) -> None:
        self.allowed = allowed
        self.reason = reason


class _FakeSyncEngine:
    def __init__(self, lxmf_hash: str = "self-peer") -> None:
        self.lxmf_hash = lxmf_hash
        self.sent: list[tuple[str, str, dict]] = []

    def send_lxmf(self, destination_hash, payload, msg_type, priority=None):
        decoded = json.loads(payload.decode("utf-8"))
        self.sent.append((destination_hash, msg_type, decoded))
        return True

    def get_lxmf_hash(self) -> str:
        return self.lxmf_hash


def _seed_partial_session(
    board_id: str,
    blob_hash: str,
    chunk_datas: list[bytes],
    *,
    stored_indexes: set[int],
    sync_engine=None,
) -> tuple[asyncio.AbstractEventLoop, payload_fetch.PendingChunkSession, ChunkManifest, list[ChunkManifestEntry]]:
    loop = asyncio.get_running_loop()
    manifest, entries = _build_manifest(board_id, blob_hash, chunk_datas)
    session = payload_fetch.register_pending_chunk_session(
        board_id,
        blob_hash,
        "text",
        "peer-a",
        loop,
        session_id=f"sess-{blob_hash[:8]}",
    )
    reassembly = ReassemblyBuffer(
        chunk_assembly_path(board_id, blob_hash),
        manifest.blob_size,
        manifest.chunk_count,
    )
    reassembly.reserve()
    for idx in sorted(stored_indexes):
        entry = entries[idx]
        reassembly.write_verified_chunk(idx, entry.offset, chunk_datas[idx])
    with session.lock:
        session.manifest = manifest
        session.entries = entries
        session.entries_by_index = {entry.chunk_index: entry for entry in entries}
        session.reassembly = reassembly
        session.stored_chunks.update(stored_indexes)
        session.sync_engine = sync_engine
    return loop, session, manifest, entries


def test_chunk_request_serves_live_partial_chunk_without_final_payload() -> None:
    async def runner() -> None:
        _repoint_retiboard_home()
        _reset_pending_chunk_registry()
        await _reset_board_pool()

        board_id = "partial-serve"
        blob_hash = "11" * 32
        chunk_datas = [b"A" * 8, b"B" * 8]
        _, session, _, _ = _seed_partial_session(
            board_id,
            blob_hash,
            chunk_datas,
            stored_indexes={0},
        )

        payload_sends: list[tuple[bytes, dict]] = []

        async def _fake_get_db(*_args, **_kwargs):
            return object()

        async def _fake_should_serve(*_args, **_kwargs):
            return _FakeDecision(True)

        async def _fail_manifest(*_args, **_kwargs):
            raise AssertionError("partial serve should not fall back to manifest loading")

        original_get_db = payload_fetch.get_board_connection
        original_should_serve = payload_fetch.should_serve_blob
        original_load_manifest = payload_fetch.load_chunk_manifest
        original_enqueue = payload_fetch._enqueue_payload_resource_send
        try:
            payload_fetch.get_board_connection = _fake_get_db
            payload_fetch.should_serve_blob = _fake_should_serve
            payload_fetch.load_chunk_manifest = _fail_manifest
            payload_fetch._enqueue_payload_resource_send = (
                lambda _identity, data, metadata: payload_sends.append((data, metadata))
            )

            sync_engine = _FakeSyncEngine()
            await payload_fetch.handle_chunk_request_lxmf(
                json.dumps(
                    {
                        "board_id": board_id,
                        "blob_hash": blob_hash,
                        "chunk_index": 0,
                        "request_id": "req-live-0",
                    }
                ),
                "peer-b",
                object(),
                sync_engine,
            )
        finally:
            payload_fetch.get_board_connection = original_get_db
            payload_fetch.should_serve_blob = original_should_serve
            payload_fetch.load_chunk_manifest = original_load_manifest
            payload_fetch._enqueue_payload_resource_send = original_enqueue
            await payload_fetch.cancel_pending_chunk_session(blob_hash)

        assert len(payload_sends) == 1
        data, metadata = payload_sends[0]
        assert data == chunk_datas[0]
        assert metadata["chunk_index"] == 0
        assert metadata["request_id"] == "req-live-0"
        assert metadata["peer_lxmf_hash"] == "self-peer"
        assert session is not None

    asyncio.run(runner())


def test_chunk_request_rejects_corrupted_live_partial_chunk() -> None:
    async def runner() -> None:
        _repoint_retiboard_home()
        _reset_pending_chunk_registry()
        await _reset_board_pool()

        board_id = "partial-corrupt"
        blob_hash = "22" * 32
        chunk_datas = [b"C" * 8, b"D" * 8]
        _, session, _, entries = _seed_partial_session(
            board_id,
            blob_hash,
            chunk_datas,
            stored_indexes={0},
        )
        with open(session.reassembly.temp_blob_path, "r+b") as handle:
            handle.seek(entries[0].offset)
            handle.write(b"Z" * entries[0].size)

        payload_sends: list[tuple[bytes, dict]] = []

        async def _fake_get_db(*_args, **_kwargs):
            return object()

        async def _fake_should_serve(*_args, **_kwargs):
            return _FakeDecision(True)

        async def _fake_load_manifest(*_args, **_kwargs):
            return None

        async def _fake_get_blob_ref(*_args, **_kwargs):
            return None

        original_get_db = payload_fetch.get_board_connection
        original_should_serve = payload_fetch.should_serve_blob
        original_load_manifest = payload_fetch.load_chunk_manifest
        original_get_blob_ref = payload_fetch.get_blob_reference
        original_enqueue = payload_fetch._enqueue_payload_resource_send
        original_read_payload = payload_fetch.read_payload
        try:
            payload_fetch.get_board_connection = _fake_get_db
            payload_fetch.should_serve_blob = _fake_should_serve
            payload_fetch.load_chunk_manifest = _fake_load_manifest
            payload_fetch.get_blob_reference = _fake_get_blob_ref
            payload_fetch.read_payload = lambda *_args, **_kwargs: None
            payload_fetch._enqueue_payload_resource_send = (
                lambda _identity, data, metadata: payload_sends.append((data, metadata))
            )

            await payload_fetch.handle_chunk_request_lxmf(
                json.dumps(
                    {
                        "board_id": board_id,
                        "blob_hash": blob_hash,
                        "chunk_index": 0,
                        "request_id": "req-corrupt-0",
                    }
                ),
                "peer-b",
                object(),
                _FakeSyncEngine(),
            )
        finally:
            payload_fetch.get_board_connection = original_get_db
            payload_fetch.should_serve_blob = original_should_serve
            payload_fetch.load_chunk_manifest = original_load_manifest
            payload_fetch.get_blob_reference = original_get_blob_ref
            payload_fetch.read_payload = original_read_payload
            payload_fetch._enqueue_payload_resource_send = original_enqueue
            await payload_fetch.cancel_pending_chunk_session(blob_hash)

        assert payload_sends == []

    asyncio.run(runner())


def test_chunk_request_does_not_serve_live_partial_when_paused() -> None:
    async def runner() -> None:
        _repoint_retiboard_home()
        _reset_pending_chunk_registry()
        await _reset_board_pool()

        board_id = "partial-paused"
        blob_hash = "33" * 32
        chunk_datas = [b"E" * 8, b"F" * 8]
        _, session, _, _ = _seed_partial_session(
            board_id,
            blob_hash,
            chunk_datas,
            stored_indexes={0},
        )
        with session.lock:
            session.paused = True

        payload_sends: list[tuple[bytes, dict]] = []

        async def _fake_get_db(*_args, **_kwargs):
            return object()

        async def _fake_should_serve(*_args, **_kwargs):
            return _FakeDecision(True)

        async def _fake_load_manifest(*_args, **_kwargs):
            return None

        async def _fake_get_blob_ref(*_args, **_kwargs):
            return None

        original_get_db = payload_fetch.get_board_connection
        original_should_serve = payload_fetch.should_serve_blob
        original_load_manifest = payload_fetch.load_chunk_manifest
        original_get_blob_ref = payload_fetch.get_blob_reference
        original_enqueue = payload_fetch._enqueue_payload_resource_send
        original_read_payload = payload_fetch.read_payload
        try:
            payload_fetch.get_board_connection = _fake_get_db
            payload_fetch.should_serve_blob = _fake_should_serve
            payload_fetch.load_chunk_manifest = _fake_load_manifest
            payload_fetch.get_blob_reference = _fake_get_blob_ref
            payload_fetch.read_payload = lambda *_args, **_kwargs: None
            payload_fetch._enqueue_payload_resource_send = (
                lambda _identity, data, metadata: payload_sends.append((data, metadata))
            )

            await payload_fetch.handle_chunk_request_lxmf(
                json.dumps(
                    {
                        "board_id": board_id,
                        "blob_hash": blob_hash,
                        "chunk_index": 0,
                        "request_id": "req-paused-0",
                    }
                ),
                "peer-b",
                object(),
                _FakeSyncEngine(),
            )
        finally:
            payload_fetch.get_board_connection = original_get_db
            payload_fetch.should_serve_blob = original_should_serve
            payload_fetch.load_chunk_manifest = original_load_manifest
            payload_fetch.get_blob_reference = original_get_blob_ref
            payload_fetch.read_payload = original_read_payload
            payload_fetch._enqueue_payload_resource_send = original_enqueue
            await payload_fetch.cancel_pending_chunk_session(blob_hash)

        assert payload_sends == []

    asyncio.run(runner())


def test_chunk_store_sends_incremental_offer_to_interested_peers() -> None:
    async def runner() -> None:
        _repoint_retiboard_home()
        _reset_pending_chunk_registry()
        await _reset_board_pool()

        board_id = "partial-offer"
        blob_hash = "44" * 32
        chunk_datas = [b"G" * 8, b"H" * 8]
        sync_engine = _FakeSyncEngine()
        _, session, manifest, entries = _seed_partial_session(
            board_id,
            blob_hash,
            chunk_datas,
            stored_indexes=set(),
            sync_engine=sync_engine,
        )
        with session.lock:
            session.interested_peers.add("peer-c")

        swarm = SwarmFetcher(
            peer_lxmf_hashes=["peer-a"],
            chunk_count=manifest.chunk_count,
            next_chunk_timeout=lambda _idx: 30.0,
            max_inflight_total=1,
            max_inflight_per_peer=1,
        )
        plan = swarm.plan_requests()[0]
        swarm.mark_request_sent(plan.request_id)
        with session.lock:
            session.swarm = swarm

        original_debounce = payload_fetch._CHUNK_OFFER_UPDATE_DEBOUNCE
        try:
            payload_fetch._CHUNK_OFFER_UPDATE_DEBOUNCE = 0.01
            payload_fetch._handle_incoming_chunk_resource(
                {
                    "board_id": board_id,
                    "blob_hash": blob_hash,
                    "chunk_index": 0,
                    "request_id": plan.request_id,
                    "peer_lxmf_hash": "peer-a",
                },
                chunk_datas[0],
            )
            await asyncio.sleep(0.05)
        finally:
            payload_fetch._CHUNK_OFFER_UPDATE_DEBOUNCE = original_debounce
            await payload_fetch.cancel_pending_chunk_session(blob_hash)

        offers = [item for item in sync_engine.sent if item[1] == payload_fetch.MSG_TYPE_CHUNK_OFFER]
        assert len(offers) == 1
        destination_hash, _, payload = offers[0]
        assert destination_hash == "peer-c"
        assert payload["blob_hash"] == blob_hash
        assert payload["chunk_count"] == manifest.chunk_count
        assert payload["complete"] is False
        assert payload["ranges"] == [[0, 0]]

    asyncio.run(runner())


def teardown_module(module) -> None:
    _repoint_retiboard_home()
    _reset_pending_chunk_registry()
    asyncio.run(_reset_board_pool())
    shutil.rmtree(_TEST_HOME, ignore_errors=True)
