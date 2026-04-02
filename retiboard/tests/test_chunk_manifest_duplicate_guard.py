# ruff: noqa: E402

from __future__ import annotations

import pytest
pytest.importorskip("aiosqlite")

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
    )

_TEST_HOME = tempfile.mkdtemp(prefix="retiboard_manifest_guard_")
os.environ["RETIBOARD_HOME"] = _TEST_HOME

import retiboard.config as config_module
import retiboard.db.database as database_module
import retiboard.db.pool as pool_module
import retiboard.sync.payload_fetch as payload_fetch
from retiboard.chunks.models import ChunkManifest, ChunkManifestEntry
from retiboard.chunks.reassembly import ReassemblyBuffer
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


def test_duplicate_manifest_response_does_not_truncate_partial_reassembly() -> None:
    async def runner() -> None:
        _repoint_retiboard_home()
        _reset_pending_chunk_registry()
        await _reset_board_pool()

        board_id = "guardboard"
        blob_hash = "34" * 32
        chunk_a = b"A" * 8
        chunk_b = b"B" * 8
        entries = [
            ChunkManifestEntry(
                blob_hash=blob_hash,
                chunk_index=0,
                offset=0,
                size=len(chunk_a),
                chunk_hash=hashlib.sha256(chunk_a).hexdigest(),
            ),
            ChunkManifestEntry(
                blob_hash=blob_hash,
                chunk_index=1,
                offset=len(chunk_a),
                size=len(chunk_b),
                chunk_hash=hashlib.sha256(chunk_b).hexdigest(),
            ),
        ]
        manifest = ChunkManifest(
            manifest_version=1,
            board_id=board_id,
            post_id="p1",
            thread_id="t1",
            blob_kind="attachments",
            blob_hash=blob_hash,
            blob_size=len(chunk_a) + len(chunk_b),
            chunk_size=8,
            chunk_count=2,
        )

        loop = asyncio.get_running_loop()
        session = payload_fetch.register_pending_chunk_session(
            board_id,
            blob_hash,
            "attachments",
            "peer-a",
            loop,
            session_id="sess-dup-manifest",
        )
        assembly_path = chunk_assembly_path(board_id, blob_hash)
        reassembly = ReassemblyBuffer(assembly_path, manifest.blob_size, manifest.chunk_count)
        reassembly.reserve()
        reassembly.write_verified_chunk(0, 0, chunk_a)

        with session.lock:
            session.manifest = manifest
            session.entries = entries
            session.entries_by_index = {entry.chunk_index: entry for entry in entries}
            session.reassembly = reassembly
            session.stored_chunks.add(0)

        original_reassembly = session.reassembly
        before = assembly_path.read_bytes()

        async def _fake_persist_offer(*_args, **_kwargs):
            return None

        async def _fake_get_db(*_args, **_kwargs):
            return object()

        async def _fake_load_session(*_args, **_kwargs):
            return None

        async def _fake_save_session(*_args, **_kwargs):
            return None

        original_persist_offer = payload_fetch._persist_peer_chunk_offer
        original_get_db = payload_fetch.get_board_connection
        original_load_session = payload_fetch.load_chunk_fetch_session
        original_save_session = payload_fetch.save_chunk_fetch_session
        try:
            payload_fetch._persist_peer_chunk_offer = _fake_persist_offer
            payload_fetch.get_board_connection = _fake_get_db
            payload_fetch.load_chunk_fetch_session = _fake_load_session
            payload_fetch.save_chunk_fetch_session = _fake_save_session

            payload = json.dumps(
                {
                    "board_id": board_id,
                    "blob_hash": blob_hash,
                    "blob_size": manifest.blob_size,
                    "chunk_size": manifest.chunk_size,
                    "chunk_count": manifest.chunk_count,
                    "blob_kind": manifest.blob_kind,
                    "entries": [
                        {
                            "blob_hash": entry.blob_hash,
                            "chunk_index": entry.chunk_index,
                            "offset": entry.offset,
                            "size": entry.size,
                            "chunk_hash": entry.chunk_hash,
                        }
                        for entry in entries
                    ],
                },
                separators=(",", ":"),
            ).encode("utf-8")

            await payload_fetch.handle_chunk_manifest_response_lxmf(payload, bytes.fromhex("12" * 32))
        finally:
            payload_fetch._persist_peer_chunk_offer = original_persist_offer
            payload_fetch.get_board_connection = original_get_db
            payload_fetch.load_chunk_fetch_session = original_load_session
            payload_fetch.save_chunk_fetch_session = original_save_session
            await payload_fetch.cancel_pending_chunk_session(blob_hash)

        after = assembly_path.read_bytes()
        assert before == after
        assert session.reassembly is original_reassembly
        assert session.stored_chunks == {0}

    asyncio.run(runner())


def test_manifest_response_persists_manifest_on_downloader() -> None:
    async def runner() -> None:
        _repoint_retiboard_home()
        _reset_pending_chunk_registry()
        await _reset_board_pool()

        board_id = "persistboard"
        blob_hash = "56" * 32
        entries = [
            ChunkManifestEntry(
                blob_hash=blob_hash,
                chunk_index=0,
                offset=0,
                size=8,
                chunk_hash=hashlib.sha256(b"A" * 8).hexdigest(),
            ),
        ]
        payload = json.dumps(
            {
                "board_id": board_id,
                "blob_hash": blob_hash,
                "blob_size": 8,
                "chunk_size": 8,
                "chunk_count": 1,
                "blob_kind": "attachments",
                "entries": [
                    {
                        "blob_hash": entry.blob_hash,
                        "chunk_index": entry.chunk_index,
                        "offset": entry.offset,
                        "size": entry.size,
                        "chunk_hash": entry.chunk_hash,
                    }
                    for entry in entries
                ],
            },
            separators=(",", ":"),
        ).encode("utf-8")

        loop = asyncio.get_running_loop()
        payload_fetch.register_pending_chunk_session(
            board_id,
            blob_hash,
            "attachments",
            "peer-a",
            loop,
            session_id="sess-persist-manifest",
        )

        saved_manifest = {}

        class _FakeReassembly:
            def __init__(self, path, blob_size, chunk_count):
                self.path = path
                self.blob_size = blob_size
                self.chunk_count = chunk_count
                self.reserved = False

            def reserve(self):
                self.reserved = True

        async def _fake_get_db(*_args, **_kwargs):
            return object()

        async def _fake_get_blob_ref(*_args, **_kwargs):
            return {"expiry_timestamp": 1234}

        async def _fake_save_manifest(_db, manifest, manifest_entries, expires_at=0):
            saved_manifest["blob_hash"] = manifest.blob_hash
            saved_manifest["chunk_count"] = manifest.chunk_count
            saved_manifest["entry_count"] = len(manifest_entries)
            saved_manifest["expires_at"] = expires_at

        async def _fake_persist_offer(*_args, **_kwargs):
            return None

        async def _fake_load_session(*_args, **_kwargs):
            return None

        async def _fake_save_session(*_args, **_kwargs):
            return None

        original_reassembly = payload_fetch.ReassemblyBuffer
        original_get_db = payload_fetch.get_board_connection
        original_get_blob_ref = payload_fetch.get_blob_reference
        original_save_manifest = payload_fetch.save_chunk_manifest
        original_persist_offer = payload_fetch._persist_peer_chunk_offer
        original_load_session = payload_fetch.load_chunk_fetch_session
        original_save_session = payload_fetch.save_chunk_fetch_session
        try:
            payload_fetch.ReassemblyBuffer = _FakeReassembly
            payload_fetch.get_board_connection = _fake_get_db
            payload_fetch.get_blob_reference = _fake_get_blob_ref
            payload_fetch.save_chunk_manifest = _fake_save_manifest
            payload_fetch._persist_peer_chunk_offer = _fake_persist_offer
            payload_fetch.load_chunk_fetch_session = _fake_load_session
            payload_fetch.save_chunk_fetch_session = _fake_save_session

            await payload_fetch.handle_chunk_manifest_response_lxmf(payload, bytes.fromhex("34" * 32))
        finally:
            payload_fetch.ReassemblyBuffer = original_reassembly
            payload_fetch.get_board_connection = original_get_db
            payload_fetch.get_blob_reference = original_get_blob_ref
            payload_fetch.save_chunk_manifest = original_save_manifest
            payload_fetch._persist_peer_chunk_offer = original_persist_offer
            payload_fetch.load_chunk_fetch_session = original_load_session
            payload_fetch.save_chunk_fetch_session = original_save_session
            await payload_fetch.cancel_pending_chunk_session(blob_hash)

        assert saved_manifest == {
            "blob_hash": blob_hash,
            "chunk_count": 1,
            "entry_count": 1,
            "expires_at": 1234,
        }

    asyncio.run(runner())


def teardown_module(module) -> None:
    _repoint_retiboard_home()
    _reset_pending_chunk_registry()
    asyncio.run(_reset_board_pool())
    shutil.rmtree(_TEST_HOME, ignore_errors=True)
