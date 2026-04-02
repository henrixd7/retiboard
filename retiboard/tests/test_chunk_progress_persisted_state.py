# ruff: noqa: E402

"""Regression tests for persisted chunk progress semantics."""

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
from retiboard.sync import payload_fetch


def test_persisted_paused_progress_reports_real_percent_without_resumed_flag() -> None:
    persisted = types.SimpleNamespace(session_id="sess-1", state="paused")
    states = [
        types.SimpleNamespace(chunk_index=0, state="stored"),
        types.SimpleNamespace(chunk_index=1, state="stored"),
        types.SimpleNamespace(chunk_index=2, state="requested"),
        types.SimpleNamespace(chunk_index=3, state="missing"),
    ]
    manifest = ChunkManifest(
        manifest_version=1,
        board_id="board-progress",
        post_id="p1",
        thread_id="t1",
        blob_kind="attachments",
        blob_hash="78" * 32,
        blob_size=1024,
        chunk_size=256,
        chunk_count=4,
    )

    async def _fake_get_db(*_args, **_kwargs):
        return object()

    async def _fake_load_latest(*_args, **_kwargs):
        return persisted

    async def _fake_load_states(*_args, **_kwargs):
        return states

    async def _fake_load_manifest(*_args, **_kwargs):
        return manifest, []

    original_get_db = payload_fetch.get_board_connection
    original_load_latest = payload_fetch.load_latest_chunk_fetch_session_for_blob
    original_load_states = payload_fetch.load_chunk_request_states
    original_load_manifest = payload_fetch.load_chunk_manifest
    original_payload_exists = payload_fetch.payload_exists
    try:
        payload_fetch.get_board_connection = _fake_get_db
        payload_fetch.load_latest_chunk_fetch_session_for_blob = _fake_load_latest
        payload_fetch.load_chunk_request_states = _fake_load_states
        payload_fetch.load_chunk_manifest = _fake_load_manifest
        payload_fetch.payload_exists = lambda *_args, **_kwargs: False

        progress = asyncio.run(payload_fetch.get_chunk_fetch_progress("board-progress", "78" * 32))
    finally:
        payload_fetch.get_board_connection = original_get_db
        payload_fetch.load_latest_chunk_fetch_session_for_blob = original_load_latest
        payload_fetch.load_chunk_request_states = original_load_states
        payload_fetch.load_chunk_manifest = original_load_manifest
        payload_fetch.payload_exists = original_payload_exists

    assert progress is not None
    assert progress["state"] == "paused"
    assert progress["stored_chunks"] == 2
    assert progress["chunk_count"] == 4
    assert progress["percent_complete"] == 50
    assert progress["requested_chunks"] == 0
    assert progress["active_requests"] == 0
    assert progress["resumed_from_persisted"] is False


def test_live_placeholder_progress_prefers_persisted_resume_snapshot() -> None:
    loop = asyncio.new_event_loop()
    session = payload_fetch.register_pending_chunk_session(
        "board-progress-live",
        "90" * 32,
        "attachments",
        "peer-a",
        loop,
        session_id="sess-live-progress",
    )
    session.candidate_peer_lxmf_hashes = ["peer-a"]

    persisted = types.SimpleNamespace(
        session_id="sess-live-progress",
        state="started",
        blob_kind="attachments",
    )
    states = [
        types.SimpleNamespace(chunk_index=0, state="stored"),
        types.SimpleNamespace(chunk_index=1, state="stored"),
        types.SimpleNamespace(chunk_index=2, state="missing"),
        types.SimpleNamespace(chunk_index=3, state="missing"),
    ]
    manifest = ChunkManifest(
        manifest_version=1,
        board_id="board-progress-live",
        post_id="p1",
        thread_id="t1",
        blob_kind="attachments",
        blob_hash="90" * 32,
        blob_size=1024,
        chunk_size=256,
        chunk_count=4,
    )

    async def _fake_get_db(*_args, **_kwargs):
        return object()

    async def _fake_load_latest(*_args, **_kwargs):
        return persisted

    async def _fake_load_states(*_args, **_kwargs):
        return states

    async def _fake_load_manifest(*_args, **_kwargs):
        return manifest, []

    original_get_db = payload_fetch.get_board_connection
    original_load_latest = payload_fetch.load_latest_chunk_fetch_session_for_blob
    original_load_states = payload_fetch.load_chunk_request_states
    original_load_manifest = payload_fetch.load_chunk_manifest
    original_payload_exists = payload_fetch.payload_exists
    try:
        payload_fetch.get_board_connection = _fake_get_db
        payload_fetch.load_latest_chunk_fetch_session_for_blob = _fake_load_latest
        payload_fetch.load_chunk_request_states = _fake_load_states
        payload_fetch.load_chunk_manifest = _fake_load_manifest
        payload_fetch.payload_exists = lambda *_args, **_kwargs: False

        progress = asyncio.run(payload_fetch.get_chunk_fetch_progress("board-progress-live", "90" * 32))
    finally:
        payload_fetch.get_board_connection = original_get_db
        payload_fetch.load_latest_chunk_fetch_session_for_blob = original_load_latest
        payload_fetch.load_chunk_request_states = original_load_states
        payload_fetch.load_chunk_manifest = original_load_manifest
        payload_fetch.payload_exists = original_payload_exists
        asyncio.run(payload_fetch.cancel_pending_chunk_session("90" * 32))
        loop.close()

    assert progress is not None
    assert progress["state"] == "started"
    assert progress["stored_chunks"] == 2
    assert progress["chunk_count"] == 4
    assert progress["percent_complete"] == 50
    assert progress["resumed_from_persisted"] is True


def test_live_placeholder_progress_falls_back_to_live_snapshot_when_no_persisted_state() -> None:
    loop = asyncio.new_event_loop()
    session = payload_fetch.register_pending_chunk_session(
        "board-progress-none",
        "91" * 32,
        "attachments",
        "peer-a",
        loop,
        session_id="sess-live-none",
    )
    session.candidate_peer_lxmf_hashes = ["peer-a"]

    async def _fake_load_persisted(*_args, **_kwargs):
        return None

    original_load_persisted = payload_fetch._load_persisted_progress_snapshot
    original_payload_exists = payload_fetch.payload_exists
    try:
        payload_fetch._load_persisted_progress_snapshot = _fake_load_persisted
        payload_fetch.payload_exists = lambda *_args, **_kwargs: False

        progress = asyncio.run(payload_fetch.get_chunk_fetch_progress("board-progress-none", "91" * 32))
    finally:
        payload_fetch._load_persisted_progress_snapshot = original_load_persisted
        payload_fetch.payload_exists = original_payload_exists
        asyncio.run(payload_fetch.cancel_pending_chunk_session("91" * 32))
        loop.close()

    assert progress is not None
    assert progress["state"] == "manifest_pending"
    assert progress["chunk_count"] == 0
    assert progress["resumed_from_persisted"] is False


def test_complete_payload_progress_reads_manifest_without_nameerror() -> None:
    manifest = ChunkManifest(
        manifest_version=1,
        board_id="board-progress-complete",
        post_id="p1",
        thread_id="t1",
        blob_kind="attachments",
        blob_hash="92" * 32,
        blob_size=1024,
        chunk_size=256,
        chunk_count=4,
    )

    async def _fake_get_db(*_args, **_kwargs):
        return object()

    async def _fake_load_manifest(*_args, **_kwargs):
        return manifest, []

    original_get_db = payload_fetch.get_board_connection
    original_load_manifest = payload_fetch.load_chunk_manifest
    original_payload_exists = payload_fetch.payload_exists
    try:
        payload_fetch.get_board_connection = _fake_get_db
        payload_fetch.load_chunk_manifest = _fake_load_manifest
        payload_fetch.payload_exists = lambda *_args, **_kwargs: True

        progress = asyncio.run(
            payload_fetch.get_chunk_fetch_progress(
                "board-progress-complete",
                "92" * 32,
            )
        )
    finally:
        payload_fetch.get_board_connection = original_get_db
        payload_fetch.load_chunk_manifest = original_load_manifest
        payload_fetch.payload_exists = original_payload_exists

    assert progress is not None
    assert progress["state"] == "complete"
    assert progress["chunk_count"] == 4
    assert progress["stored_chunks"] == 4
    assert progress["percent_complete"] == 100
