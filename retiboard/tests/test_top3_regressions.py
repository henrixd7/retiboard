# ruff: noqa: E402

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import sys
import tempfile
import types

from fastapi import FastAPI
from fastapi.testclient import TestClient


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TEST_HOME = tempfile.mkdtemp(prefix="retiboard_top3_test_")
os.environ["RETIBOARD_HOME"] = _TEST_HOME

_ORIGINAL_PAYLOAD_FETCH = sys.modules.get("retiboard.sync.payload_fetch")

payload_fetch_stub = types.ModuleType("retiboard.sync.payload_fetch")


async def _stub_cancel(*args, **kwargs):
    return False


async def _stub_progress(*args, **kwargs):
    return None


async def _stub_pause(*args, **kwargs):
    return False


async def _stub_resume(*args, **kwargs):
    return False


payload_fetch_stub.cancel_chunk_fetch = _stub_cancel
payload_fetch_stub.get_chunk_fetch_progress = _stub_progress
payload_fetch_stub.pause_chunk_fetch = _stub_pause
payload_fetch_stub.resume_chunk_fetch = _stub_resume
sys.modules["retiboard.sync.payload_fetch"] = payload_fetch_stub


from retiboard.db.database import (
    board_dir,
    get_post,
    open_board_db,
    open_existing_board_db,
    post_exists,
    save_board_config,
)
from retiboard.db.models import Board, PostMetadata
from retiboard.api.routes.posts import create_posts_router
from retiboard.pruning.pruner import prune_board

if _ORIGINAL_PAYLOAD_FETCH is None:
    sys.modules.pop("retiboard.sync.payload_fetch", None)
else:
    sys.modules["retiboard.sync.payload_fetch"] = _ORIGINAL_PAYLOAD_FETCH


def make_board(board_id: str = "testboard") -> Board:
    return Board(
        board_id=board_id,
        display_name="Test Board",
        text_only=False,
        default_ttl_seconds=43200,
        bump_decay_rate=3600,
        pow_difficulty=0,
        key_material="deadbeef" * 4,
        announce_version=2,
    )


def make_post(
    *,
    post_id: str,
    thread_id: str,
    parent_id: str = "",
    timestamp: int,
    expiry_timestamp: int,
) -> PostMetadata:
    return PostMetadata(
        post_id=post_id,
        thread_id=thread_id,
        parent_id=parent_id,
        timestamp=timestamp,
        expiry_timestamp=expiry_timestamp,
        bump_flag=True,
        content_hash=f"{post_id:0<64}"[:64],
        payload_size=16,
        has_attachments=False,
        text_only=False,
        identity_hash="",
        pow_nonce="",
        public_key="",
        encrypted_pings=[],
        edit_signature="",
        thread_last_activity=timestamp if post_id == thread_id else 0,
        is_abandoned=False,
    )


def make_posts_app() -> FastAPI:
    app = FastAPI()
    app.include_router(create_posts_router(board_manager=None, sync_engine=None))
    return app


def test_open_existing_board_db_does_not_create_missing_board() -> None:
    board_id = "missing_board"
    assert board_dir(board_id).exists() is False

    try:
        asyncio.run(open_existing_board_db(board_id))
        raise AssertionError("Expected FileNotFoundError")
    except FileNotFoundError:
        pass

    assert board_dir(board_id).exists() is False


def test_prune_keeps_all_posts_in_active_thread() -> None:
    board = make_board("active_prune")
    async def _run() -> None:
        db = await open_board_db(board.board_id)
        try:
            await save_board_config(db, board)

            op = make_post(
                post_id="thread1",
                thread_id="thread1",
                timestamp=1_000,
                expiry_timestamp=20_000,
            )
            op.thread_last_activity = 5_000

            reply = make_post(
                post_id="reply1",
                thread_id="thread1",
                parent_id="thread1",
                timestamp=1_200,
                expiry_timestamp=20_000,
            )

            await db.execute(
                """
                INSERT INTO posts (
                    post_id, thread_id, parent_id, timestamp, expiry_timestamp,
                    bump_flag, content_hash, payload_size,
                    attachment_content_hash, attachment_payload_size,
                    has_attachments, text_only,
                    identity_hash, pow_nonce, thread_last_activity, is_abandoned
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', 0, 0, 0, '', '', ?, 0)
                """,
                (
                    op.post_id,
                    op.thread_id,
                    op.parent_id,
                    op.timestamp,
                    op.expiry_timestamp,
                    1,
                    op.content_hash,
                    op.payload_size,
                    op.thread_last_activity,
                ),
            )
            await db.execute(
                """
                INSERT INTO posts (
                    post_id, thread_id, parent_id, timestamp, expiry_timestamp,
                    bump_flag, content_hash, payload_size,
                    attachment_content_hash, attachment_payload_size,
                    has_attachments, text_only,
                    identity_hash, pow_nonce, thread_last_activity, is_abandoned
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', 0, 0, 0, '', '', ?, 0)
                """,
                (
                    reply.post_id,
                    reply.thread_id,
                    reply.parent_id,
                    reply.timestamp,
                    reply.expiry_timestamp,
                    1,
                    reply.content_hash,
                    reply.payload_size,
                    0,
                ),
            )
            await db.commit()
            result = await prune_board(board.board_id, now=10_000)
            assert result.threads_deleted == 0
            assert await get_post(db, "thread1") is not None
            assert await get_post(db, "reply1") is not None
        finally:
            await db.close()

    asyncio.run(_run())


def test_posts_list_missing_board_returns_404_without_creating_board() -> None:
    board_id = "ghost_board"
    app = make_posts_app()

    with TestClient(app) as client:
        response = client.get(f"/api/boards/{board_id}/posts")

    assert response.status_code == 404
    assert board_dir(board_id).exists() is False


def test_create_post_rejects_missing_media_without_persisting_metadata() -> None:
    board = make_board("post_atomic_media")
    async def _setup() -> None:
        db = await open_board_db(board.board_id)
        try:
            await save_board_config(db, board)
        finally:
            await db.close()
    asyncio.run(_setup())

    app = make_posts_app()
    metadata = {
        "post_id": "p1",
        "thread_id": "p1",
        "parent_id": "",
        "timestamp": 1_234_567,
        "bump_flag": True,
        "content_hash": "a" * 64,
        "payload_size": 4,
        "attachment_content_hash": "b" * 64,
        "attachment_payload_size": 4,
        "has_attachments": True,
        "attachment_count": 1,
        "text_only": False,
        "identity_hash": "",
        "pow_nonce": "",
        "public_key": "",
        "encrypted_pings": [],
        "edit_signature": "",
    }

    files = {
        "payload": ("payload.bin", io.BytesIO(b"test"), "application/octet-stream"),
    }

    with TestClient(app) as client:
        response = client.post(
            f"/api/boards/{board.board_id}/posts",
            data={"metadata": json.dumps(metadata)},
            files=files,
        )

    assert response.status_code == 400

    async def _check() -> None:
        db = await open_board_db(board.board_id)
        try:
            assert await post_exists(db, "p1") is False
        finally:
            await db.close()
    asyncio.run(_check())


def test_create_post_rolls_back_when_payload_write_fails() -> None:
    board = make_board("post_atomic_write_fail")
    async def _setup() -> None:
        db = await open_board_db(board.board_id)
        try:
            await save_board_config(db, board)
        finally:
            await db.close()
    asyncio.run(_setup())

    import retiboard.api.routes.posts as posts_module

    app = make_posts_app()
    metadata = {
        "post_id": "p2",
        "thread_id": "p2",
        "parent_id": "",
        "timestamp": 1_234_567,
        "bump_flag": True,
        "content_hash": "9f86d081884c7d659a2feaa0c55ad015"
                        "a3bf4f1b2b0b822cd15d6c15b0f00a08",
        "payload_size": 4,
        "attachment_content_hash": "",
        "attachment_payload_size": 0,
        "has_attachments": False,
        "attachment_count": 0,
        "text_only": False,
        "identity_hash": "",
        "pow_nonce": "",
        "public_key": "",
        "encrypted_pings": [],
        "edit_signature": "",
    }

    original_write_payload = posts_module.write_payload

    def _failing_write_payload(*args, **kwargs):
        raise OSError("simulated write failure")

    posts_module.write_payload = _failing_write_payload
    try:
        with TestClient(app) as client:
            response = client.post(
                f"/api/boards/{board.board_id}/posts",
                data={"metadata": json.dumps(metadata)},
                files={
                    "payload": (
                        "payload.bin",
                        io.BytesIO(b"test"),
                        "application/octet-stream",
                    ),
                },
            )
    finally:
        posts_module.write_payload = original_write_payload

    assert response.status_code >= 500

    async def _check() -> None:
        db = await open_board_db(board.board_id)
        try:
            assert await post_exists(db, "p2") is False
        finally:
            await db.close()
    asyncio.run(_check())


def test_create_post_persists_attachment_count() -> None:
    board = make_board("post_attachment_count")

    async def _setup() -> None:
        db = await open_board_db(board.board_id)
        try:
            await save_board_config(db, board)
        finally:
            await db.close()

    asyncio.run(_setup())

    import hashlib

    payload_bytes = b"text"
    attachment_bytes = b"blob"
    metadata = {
        "post_id": "p3",
        "thread_id": "p3",
        "parent_id": "",
        "timestamp": 1_234_567,
        "bump_flag": True,
        "content_hash": hashlib.sha256(payload_bytes).hexdigest(),
        "payload_size": len(payload_bytes),
        "attachment_content_hash": hashlib.sha256(attachment_bytes).hexdigest(),
        "attachment_payload_size": len(attachment_bytes),
        "has_attachments": True,
        "attachment_count": 3,
        "text_only": False,
        "identity_hash": "",
        "pow_nonce": "",
        "public_key": "",
        "encrypted_pings": [],
        "edit_signature": "",
    }

    with TestClient(make_posts_app()) as client:
        response = client.post(
            f"/api/boards/{board.board_id}/posts",
            data={"metadata": json.dumps(metadata)},
            files={
                "payload": ("payload.bin", io.BytesIO(payload_bytes), "application/octet-stream"),
                "attachment_payload": ("attachment_payload.bin", io.BytesIO(attachment_bytes), "application/octet-stream"),
            },
        )

    assert response.status_code == 201
    assert response.json()["attachment_count"] == 3

    async def _check() -> None:
        db = await open_board_db(board.board_id)
        try:
            post = await get_post(db, "p3")
            assert post is not None
            assert post.attachment_count == 3
        finally:
            await db.close()

    asyncio.run(_check())


def test_create_post_rejects_oversized_attachment_payload() -> None:
    board = make_board("post_attachment_too_large")

    async def _setup() -> None:
        db = await open_board_db(board.board_id)
        try:
            await save_board_config(db, board)
        finally:
            await db.close()

    asyncio.run(_setup())

    import hashlib
    import retiboard.api.routes.posts as posts_module

    payload_bytes = b"text"
    attachment_bytes = b"abcdefghi"
    metadata = {
        "post_id": "p4",
        "thread_id": "p4",
        "parent_id": "",
        "timestamp": 1_234_567,
        "bump_flag": True,
        "content_hash": hashlib.sha256(payload_bytes).hexdigest(),
        "payload_size": len(payload_bytes),
        "attachment_content_hash": hashlib.sha256(attachment_bytes).hexdigest(),
        "attachment_payload_size": len(attachment_bytes),
        "has_attachments": True,
        "attachment_count": 1,
        "text_only": False,
        "identity_hash": "",
        "pow_nonce": "",
        "public_key": "",
        "encrypted_pings": [],
        "edit_signature": "",
    }

    original_get_max_payload_size = posts_module.get_max_payload_size
    posts_module.get_max_payload_size = lambda *args, **kwargs: 8
    try:
        with TestClient(make_posts_app()) as client:
            response = client.post(
                f"/api/boards/{board.board_id}/posts",
                data={"metadata": json.dumps(metadata)},
                files={
                    "payload": ("payload.bin", io.BytesIO(payload_bytes), "application/octet-stream"),
                    "attachment_payload": ("attachment_payload.bin", io.BytesIO(attachment_bytes), "application/octet-stream"),
                },
            )
    finally:
        posts_module.get_max_payload_size = original_get_max_payload_size

    assert response.status_code == 413

    async def _check() -> None:
        db = await open_board_db(board.board_id)
        try:
            assert await post_exists(db, "p4") is False
        finally:
            await db.close()

    asyncio.run(_check())


def test_create_post_rejects_oversized_text_payload() -> None:
    board = make_board("post_text_too_large")

    async def _setup() -> None:
        db = await open_board_db(board.board_id)
        try:
            await save_board_config(db, board)
        finally:
            await db.close()

    asyncio.run(_setup())

    import hashlib
    import retiboard.api.routes.posts as posts_module

    payload_bytes = b"abcdefghi"
    metadata = {
        "post_id": "p5",
        "thread_id": "p5",
        "parent_id": "",
        "timestamp": 1_234_567,
        "bump_flag": True,
        "content_hash": hashlib.sha256(payload_bytes).hexdigest(),
        "payload_size": len(payload_bytes),
        "attachment_content_hash": "",
        "attachment_payload_size": 0,
        "has_attachments": False,
        "attachment_count": 0,
        "text_only": False,
        "identity_hash": "",
        "pow_nonce": "",
        "public_key": "",
        "encrypted_pings": [],
        "edit_signature": "",
    }

    original_get_max_payload_size = posts_module.get_max_payload_size
    posts_module.get_max_payload_size = lambda *args, **kwargs: 8
    try:
        with TestClient(make_posts_app()) as client:
            response = client.post(
                f"/api/boards/{board.board_id}/posts",
                data={"metadata": json.dumps(metadata)},
                files={
                    "payload": ("payload.bin", io.BytesIO(payload_bytes), "application/octet-stream"),
                },
            )
    finally:
        posts_module.get_max_payload_size = original_get_max_payload_size

    assert response.status_code == 413

    async def _check() -> None:
        db = await open_board_db(board.board_id)
        try:
            assert await post_exists(db, "p5") is False
        finally:
            await db.close()

    asyncio.run(_check())


def teardown_module(module) -> None:
    shutil.rmtree(_TEST_HOME, ignore_errors=True)
