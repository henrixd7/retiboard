# ruff: noqa: E402

"""Regression tests for cancelling active chunk resource transfers."""

from __future__ import annotations

import asyncio
import json
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
        Resource=types.SimpleNamespace(COMPLETE=1),
    )

from retiboard.sync import payload_fetch
from retiboard.chunks.models import ChunkFetchState
from retiboard.chunks.swarm import SwarmFetcher


class _FakeResource:
    def __init__(self) -> None:
        self.cancel_calls = 0

    def cancel(self) -> None:
        self.cancel_calls += 1


def test_chunk_cancel_cancels_active_outbound_resource() -> None:
    resource = _FakeResource()
    request_id = "req-cancel-active"

    with payload_fetch._payload_sender_lock:
        payload_fetch._active_outbound_chunk_resources[request_id] = resource
        payload_fetch._cancelled_outbound_chunk_requests.discard(request_id)

    asyncio.run(
        payload_fetch.handle_chunk_cancel_lxmf(
            json.dumps({"request_id": request_id}),
            "peer-a",
            None,
            None,
        )
    )

    try:
        assert resource.cancel_calls == 1
        with payload_fetch._payload_sender_lock:
            assert request_id in payload_fetch._cancelled_outbound_chunk_requests
    finally:
        with payload_fetch._payload_sender_lock:
            payload_fetch._active_outbound_chunk_resources.pop(request_id, None)
            payload_fetch._cancelled_outbound_chunk_requests.discard(request_id)


def test_chunk_cancel_releases_sender_lock_before_cancelling_resource() -> None:
    request_id = "req-cancel-lock-order"

    class _LockAwareResource:
        def __init__(self) -> None:
            self.cancel_calls = 0
            self.lock_was_free = False

        def cancel(self) -> None:
            self.cancel_calls += 1
            acquired = payload_fetch._payload_sender_lock.acquire(blocking=False)
            self.lock_was_free = acquired
            if acquired:
                payload_fetch._payload_sender_lock.release()

    resource = _LockAwareResource()

    with payload_fetch._payload_sender_lock:
        payload_fetch._active_outbound_chunk_resources[request_id] = resource
        payload_fetch._cancelled_outbound_chunk_requests.discard(request_id)

    asyncio.run(
        payload_fetch.handle_chunk_cancel_lxmf(
            json.dumps({"request_id": request_id}),
            "peer-a",
            None,
            None,
        )
    )

    try:
        assert resource.cancel_calls == 1
        assert resource.lock_was_free is True
    finally:
        with payload_fetch._payload_sender_lock:
            payload_fetch._active_outbound_chunk_resources.pop(request_id, None)
            payload_fetch._cancelled_outbound_chunk_requests.discard(request_id)


def test_incomplete_paused_chunk_resource_is_not_fatal() -> None:
    loop = asyncio.new_event_loop()
    try:
        session = payload_fetch.register_pending_chunk_session(
            "board-pause-benign",
            "ef" * 32,
            "attachments",
            "peer-a",
            loop,
            session_id="sess-benign",
        )
        with session.lock:
            session.paused = True
            session.cancelled_request_ids.add("req-benign")

        fail_calls: list[tuple[str, str]] = []
        signal_calls: list[str] = []
        original_fail = payload_fetch.fail_pending_chunk_session
        original_signal = payload_fetch.signal_fetch_complete
        try:
            payload_fetch.fail_pending_chunk_session = lambda blob_hash, reason: fail_calls.append((blob_hash, reason))
            payload_fetch.signal_fetch_complete = lambda blob_hash: signal_calls.append(blob_hash)

            payload_fetch._resource_concluded_callback(
                types.SimpleNamespace(
                    metadata=json.dumps({
                        "board_id": "board-pause-benign",
                        "blob_hash": "ef" * 32,
                        "chunk_index": 0,
                        "request_id": "req-benign",
                    }).encode("utf-8"),
                    status=0,
                    data=b"",
                )
            )
        finally:
            payload_fetch.fail_pending_chunk_session = original_fail
            payload_fetch.signal_fetch_complete = original_signal

        assert fail_calls == []
        assert signal_calls == []
    finally:
        loop.close()


def test_resource_started_callback_cancels_paused_chunk_resource() -> None:
    loop = asyncio.new_event_loop()
    try:
        session = payload_fetch.register_pending_chunk_session(
            "board-started-pause",
            "12" * 32,
            "attachments",
            "peer-a",
            loop,
            session_id="sess-started-pause",
        )
        with session.lock:
            session.paused = True
            session.swarm = types.SimpleNamespace(lookup_request=lambda request_id: object())

        class _IncomingResource:
            def __init__(self) -> None:
                self.request_id = "req-started-pause"
                self.metadata = {
                    "board_id": "board-started-pause",
                    "blob_hash": "12" * 32,
                    "request_id": self.request_id,
                }
                self.cancel_calls = 0
                self.session_lock_was_free = False

            def cancel(self) -> None:
                self.cancel_calls += 1
                acquired = session.lock.acquire(blocking=False)
                self.session_lock_was_free = acquired
                if acquired:
                    session.lock.release()

        resource = _IncomingResource()
        try:
            payload_fetch._resource_started_callback(resource)
            assert resource.cancel_calls == 1
            assert resource.session_lock_was_free is True
            with payload_fetch._payload_sender_lock:
                assert payload_fetch._active_inbound_chunk_resources.get(resource.request_id) is resource
        finally:
            with payload_fetch._payload_sender_lock:
                payload_fetch._active_inbound_chunk_resources.pop(resource.request_id, None)
    finally:
        loop.close()


def test_pause_chunk_fetch_cancels_active_inbound_resources() -> None:
    loop = asyncio.new_event_loop()
    try:
        session = payload_fetch.register_pending_chunk_session(
            "board-pause-inbound",
            "34" * 32,
            "attachments",
            "peer-a",
            loop,
            session_id="sess-pause-inbound",
        )

        class _InboundResource:
            def __init__(self) -> None:
                self.cancel_calls = 0
                self.sender_lock_was_free = False
                self.session_lock_was_free = False

            def cancel(self) -> None:
                self.cancel_calls += 1
                sender_acquired = payload_fetch._payload_sender_lock.acquire(blocking=False)
                self.sender_lock_was_free = sender_acquired
                if sender_acquired:
                    payload_fetch._payload_sender_lock.release()
                session_acquired = session.lock.acquire(blocking=False)
                self.session_lock_was_free = session_acquired
                if session_acquired:
                    session.lock.release()

        resource = _InboundResource()
        swarm = SwarmFetcher(
            peer_lxmf_hashes=["peer-a"],
            chunk_count=1,
            next_chunk_timeout=lambda _: 30.0,
            max_inflight_total=1,
            max_inflight_per_peer=1,
        )
        plan = swarm.plan_requests()[0]
        swarm.mark_request_sent(plan.request_id)
        with session.lock:
            session.swarm = swarm

        with payload_fetch._payload_sender_lock:
            payload_fetch._active_inbound_chunk_resources[plan.request_id] = resource

        async def _fake_db(*_args, **_kwargs):
            return object()

        async def _fake_save(*_args, **_kwargs):
            return None

        original_get_db = payload_fetch.get_board_connection
        original_save = payload_fetch.save_chunk_fetch_session
        try:
            payload_fetch.get_board_connection = _fake_db
            payload_fetch.save_chunk_fetch_session = _fake_save
            asyncio.run(payload_fetch.pause_chunk_fetch("board-pause-inbound", "34" * 32))
        finally:
            payload_fetch.get_board_connection = original_get_db
            payload_fetch.save_chunk_fetch_session = original_save

        try:
            assert resource.cancel_calls == 1
            assert resource.sender_lock_was_free is True
            assert resource.session_lock_was_free is True
            assert session.swarm is not None
            assert session.swarm.active_request_count() == 0
            assert session.swarm.peers["peer-a"].in_flight == 0
            assert session.swarm.chunks[0].state == ChunkFetchState.CANCELLED
            assert plan.request_id in session.cancelled_request_ids
        finally:
            with payload_fetch._payload_sender_lock:
                payload_fetch._active_inbound_chunk_resources.pop(plan.request_id, None)
    finally:
        loop.close()


def test_large_chunked_pause_does_not_fall_back_to_whole_blob_fetch() -> None:
    class _PeerTracker:
        def get_fetch_peers(self, _board_id):
            return [types.SimpleNamespace(lxmf_hash="peer-a", identity=object())]

    async def _fake_chunked(*_args, **_kwargs):
        return payload_fetch._CHUNK_FETCH_RESULT_PAUSED

    def _unexpected_register(*_args, **_kwargs):
        raise AssertionError("whole-blob fallback should not be entered for paused chunked fetch")

    original_chunked = payload_fetch.fetch_payload_from_peers_chunked
    original_register = payload_fetch.register_pending_fetch
    try:
        payload_fetch.fetch_payload_from_peers_chunked = _fake_chunked
        payload_fetch.register_pending_fetch = _unexpected_register
        result = asyncio.run(
            payload_fetch.fetch_payload_from_peers(
                "board-large",
                "56" * 32,
                _PeerTracker(),
                self_lxmf_hash="self-peer",
                sync_engine=object(),
                expected_size=payload_fetch._CHUNK_FETCH_THRESHOLD,
            )
        )
    finally:
        payload_fetch.fetch_payload_from_peers_chunked = original_chunked
        payload_fetch.register_pending_fetch = original_register

    assert result is False
