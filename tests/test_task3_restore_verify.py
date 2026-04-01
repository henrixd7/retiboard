# ruff: noqa: E402

from __future__ import annotations

import pytest
pytest.importorskip("aiosqlite")

import asyncio
import hashlib
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TEST_HOME = tempfile.mkdtemp(prefix="retiboard_task3_")
os.environ["RETIBOARD_HOME"] = _TEST_HOME

from retiboard.chunks.models import ChunkFetchSession, ChunkManifest, ChunkManifestEntry, ChunkRequestStateRecord
import retiboard.config as config_module
import retiboard.db.database as database_module
import retiboard.db.pool as pool_module
import retiboard.sync.payload_fetch as payload_fetch_module
from retiboard.db.database import (
    load_chunk_request_states,
    open_board_db,
    save_chunk_fetch_session,
    save_chunk_manifest,
    save_chunk_request_state,
)
from retiboard.storage.payloads import chunk_assembly_path
from retiboard.sync.payload_fetch import (
    _restore_chunk_session_state,
    cancel_pending_chunk_session,
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


def _reset_pending_chunk_registry() -> None:
    with payload_fetch_module._pending_chunk_lock:
        payload_fetch_module._pending_chunk_sessions.clear()
        payload_fetch_module._pending_chunk_by_blob.clear()


def _mk_entry(blob_hash: str, idx: int, offset: int, data: bytes) -> ChunkManifestEntry:
    return ChunkManifestEntry(
        blob_hash=blob_hash,
        chunk_index=idx,
        offset=offset,
        size=len(data),
        chunk_hash=hashlib.sha256(data).hexdigest(),
    )


def test_restore_verifies_stored_chunks_on_disk() -> None:
    async def runner() -> None:
        _repoint_retiboard_home()
        _reset_pending_chunk_registry()
        await _reset_board_pool()
        board_id = "task3board"
        blob_hash = "ab" * 32
        chunk_datas = [
            b"A" * 8,
            b"B" * 8,
            b"C" * 8,
            b"D" * 8,
        ]
        entries = []
        offset = 0
        for idx, data in enumerate(chunk_datas):
            entries.append(_mk_entry(blob_hash, idx, offset, data))
            offset += len(data)
        manifest = ChunkManifest(
            manifest_version=1,
            board_id=board_id,
            post_id="p1",
            thread_id="t1",
            blob_kind="attachments",
            blob_hash=blob_hash,
            blob_size=offset,
            chunk_size=8,
            chunk_count=len(entries),
            created_at=int(time.time()),
        )

        db = await open_board_db(board_id)
        try:
            await save_chunk_manifest(db, manifest, entries, expires_at=int(time.time()) + 3600)
            await save_chunk_fetch_session(
                db,
                ChunkFetchSession(
                    session_id="sess-task3",
                    board_id=board_id,
                    blob_hash=blob_hash,
                    blob_kind="attachments",
                    state="fetching",
                    started_at=int(time.time()),
                    updated_at=int(time.time()),
                    expires_at=int(time.time()) + 3600,
                    request_peer_lxmf_hash="peer-a",
                ),
            )
            for idx in range(4):
                await save_chunk_request_state(
                    db,
                    ChunkRequestStateRecord(
                        session_id="sess-task3",
                        chunk_index=idx,
                        state="stored" if idx < 3 else "missing",
                        attempt_count=1,
                        updated_at=int(time.time()),
                    ),
                )
        finally:
            await db.close()

        assembly = chunk_assembly_path(board_id, blob_hash)
        assembly.parent.mkdir(parents=True, exist_ok=True)
        with open(assembly, "wb") as handle:
            handle.truncate(manifest.blob_size)
        with open(assembly, "r+b") as handle:
            # chunk 0 correct
            handle.seek(entries[0].offset)
            handle.write(chunk_datas[0])
            # chunk 1 correct
            handle.seek(entries[1].offset)
            handle.write(chunk_datas[1])
            # chunk 2 corrupted on disk
            handle.seek(entries[2].offset)
            handle.write(b"X" * entries[2].size)
            # chunk 3 intentionally absent / zero-filled and DB says missing already

        loop = asyncio.get_running_loop()
        session = register_pending_chunk_session(
            board_id,
            blob_hash,
            "attachments",
            "peer-a",
            loop,
            session_id="sess-task3",
        )
        try:
            restored = await _restore_chunk_session_state(session)
            assert restored is True
            assert session.resumed_from_persisted is True
            assert session.stored_chunks == {0, 1}
            assert session.reassembly is not None
            assert session.reassembly.is_complete() is False

            db = await open_board_db(board_id)
            try:
                states = await load_chunk_request_states(db, "sess-task3")
            finally:
                await db.close()
            by_idx = {row.chunk_index: row for row in states}
            assert by_idx[0].state == "stored"
            assert by_idx[1].state == "stored"
            assert by_idx[2].state == "missing"
            assert by_idx[3].state == "missing"
        finally:
            await cancel_pending_chunk_session(blob_hash)

    asyncio.run(runner())



def teardown_module(module) -> None:
    _repoint_retiboard_home()
    _reset_pending_chunk_registry()
    asyncio.run(_reset_board_pool())
    shutil.rmtree(_TEST_HOME, ignore_errors=True)
