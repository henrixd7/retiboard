# ruff: noqa: E402

"""Phase 1 chunk transport foundation tests.

Covers:
    1. Deterministic post-encryption splitting
    2. Manifest persistence round-trip
    3. Chunk pre-validation success/failure
    4. Random-access reassembly + final hash verification
    5. Prune cleanup for chunk metadata/cache
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TEST_HOME = tempfile.mkdtemp(prefix="retiboard_chunk_test_")
os.environ["RETIBOARD_HOME"] = _TEST_HOME

from retiboard.chunks.chunker import build_chunk_manifest
from retiboard.chunks.reassembly import ReassemblyBuffer
from retiboard.chunks.validator import ChunkValidationError, ChunkValidator
from retiboard.db.database import (
    open_board_db,
    save_board_config,
    save_chunk_manifest,
    load_chunk_manifest,
)
from retiboard.db.models import Board
from retiboard.pruning.pruner import prune_board
from retiboard.storage.payloads import chunk_assembly_path, chunk_cache_dir, write_payload


def make_board(board_id: str = "chunkboard") -> Board:
    return Board(
        board_id=board_id,
        display_name="Chunk Board",
        text_only=False,
        default_ttl_seconds=43200,
        bump_decay_rate=3600,
        pow_difficulty=0,
        announce_version=2,
        peer_lxmf_hash="",
    )


async def test_manifest_roundtrip() -> None:
    board = make_board("chunk_manifest")
    db = await open_board_db(board.board_id)
    try:
        await save_board_config(db, board)
        blob = (b"0123456789abcdef" * 4096) + b"tail"
        manifest, entries = build_chunk_manifest(
            board_id=board.board_id,
            post_id="p1",
            thread_id="t1",
            blob_kind="text",
            blob=blob,
            chunk_size=4096,
        )
        await save_chunk_manifest(db, manifest, entries, expires_at=int(time.time()) + 60)
        loaded = await load_chunk_manifest(db, manifest.blob_hash)
        assert loaded is not None
        loaded_manifest, loaded_entries = loaded
        assert loaded_manifest.blob_hash == manifest.blob_hash
        assert loaded_manifest.chunk_count == manifest.chunk_count
        assert [e.chunk_hash for e in loaded_entries] == [e.chunk_hash for e in entries]
        print("  [01] manifest round-trip PASS")
    finally:
        await db.close()


async def test_validation_and_reassembly() -> None:
    board = make_board("chunk_reassembly")
    db = await open_board_db(board.board_id)
    try:
        await save_board_config(db, board)
        blob = hashlib.sha256(b"seed").digest() * 4096
        manifest, entries = build_chunk_manifest(
            board_id=board.board_id,
            post_id="p2",
            thread_id="t2",
            blob_kind="attachments",
            blob=blob,
            chunk_size=8192,
        )
        validator = ChunkValidator()
        by_index = {entry.chunk_index: entry for entry in entries}
        temp_path = chunk_assembly_path(board.board_id, manifest.blob_hash)
        buf = ReassemblyBuffer(temp_path, manifest.blob_size, manifest.chunk_count)
        buf.reserve()

        chunks = []
        for entry in entries:
            part = blob[entry.offset: entry.offset + entry.size]
            chunks.append((entry, part))

        for entry, part in reversed(chunks):
            checked = validator.prevalidate(
                manifest=manifest,
                entries_by_index=by_index,
                chunk_index=entry.chunk_index,
                peer_lxmf_hash="peerA",
                assigned_peer_lxmf_hash="peerA",
                data=part,
            )
            assert checked.chunk_hash == entry.chunk_hash
            buf.write_verified_chunk(entry.chunk_index, entry.offset, part)

        assert buf.is_complete() is True
        final_path = Path(_TEST_HOME) / "final.bin"
        buf.finalize(manifest.blob_hash, final_path)
        assert final_path.read_bytes() == blob

        bad = bytearray(chunks[0][1])
        bad[0] ^= 0xFF
        try:
            validator.prevalidate(
                manifest=manifest,
                entries_by_index=by_index,
                chunk_index=chunks[0][0].chunk_index,
                peer_lxmf_hash="peerA",
                assigned_peer_lxmf_hash="peerA",
                data=bytes(bad),
            )
            raise AssertionError("Expected ChunkValidationError")
        except ChunkValidationError:
            pass

        print("  [02] validation + reassembly PASS")
    finally:
        await db.close()


async def test_prune_removes_chunk_state() -> None:
    board = make_board("chunk_prune")
    db = await open_board_db(board.board_id)
    try:
        await save_board_config(db, board)
        blob = b"opaque-encrypted-blob" * 1024
        manifest, entries = build_chunk_manifest(
            board_id=board.board_id,
            post_id="thread1",
            thread_id="thread1",
            blob_kind="text",
            blob=blob,
            chunk_size=2048,
        )
        await save_chunk_manifest(db, manifest, entries, expires_at=10)
        write_payload(board.board_id, manifest.blob_hash, blob)
        cache_dir = chunk_cache_dir(board.board_id, manifest.blob_hash)
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "0.part").write_bytes(blob[:128])

        await db.execute(
            """
            INSERT INTO posts (
                post_id, thread_id, parent_id, timestamp, expiry_timestamp,
                bump_flag, content_hash, payload_size,
                attachment_content_hash, attachment_payload_size,
                has_attachments, text_only, identity_hash, pow_nonce,
                thread_last_activity, is_abandoned
            ) VALUES (?, ?, '', ?, ?, 1, ?, ?, '', 0, 0, 0, '', '', ?, 0)
            """,
            ("thread1", "thread1", 1, 2, manifest.blob_hash, len(blob), 1),
        )
        await db.commit()
    finally:
        await db.close()

    result = await prune_board(board.board_id, now=10000)
    assert result.threads_deleted == 1
    assert cache_dir.exists() is False
    db = await open_board_db(board.board_id)
    try:
        loaded = await load_chunk_manifest(db, manifest.blob_hash)
        assert loaded is None
    finally:
        await db.close()
    print("  [03] prune cleanup PASS")


async def main() -> None:
    await test_manifest_roundtrip()
    await test_validation_and_reassembly()
    await test_prune_removes_chunk_state()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        shutil.rmtree(_TEST_HOME, ignore_errors=True)
