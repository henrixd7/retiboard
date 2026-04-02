from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from retiboard.sync.payload_scheduler import PayloadFetchScheduler


def test_manual_media_bypasses_bulk_classification() -> None:
    scheduler = PayloadFetchScheduler()
    decision = scheduler.classify(
        blob_kind='attachments',
        expected_size=64 * 1024 * 1024,
        manual_override=True,
    )
    assert decision.allowed_manual is True
    assert decision.priority_class == 'manual'


def test_bulk_request_preserves_room_for_interactive_work() -> None:
    scheduler = PayloadFetchScheduler()
    interactive = scheduler.register_session(
        session_id='s1',
        blob_hash='aa' * 32,
        blob_kind='text',
        expected_size=4096,
    )
    bulk = scheduler.register_session(
        session_id='s2',
        blob_hash='bb' * 32,
        blob_kind='attachments',
        expected_size=32 * 1024 * 1024,
        manual_override=False,
    )
    assert interactive.priority_class == 'interactive'
    assert bulk.priority_class == 'bulk'

    assert scheduler.try_acquire_request('s1', 'r1') is True
    assert scheduler.try_acquire_request('s1', 'r2') is True
    assert scheduler.try_acquire_request('s2', 'r3') is True
    assert scheduler.try_acquire_request('s2', 'r4') is True
    # Third bulk request should be denied by bulk cap (v3.6.4: cap=2).
    assert scheduler.try_acquire_request('s2', 'r5') is False


def test_bulk_ages_into_progress_when_capacity_is_available() -> None:
    scheduler = PayloadFetchScheduler()
    scheduler.register_session(
        session_id='bulk',
        blob_hash='cc' * 32,
        blob_kind='attachments',
        expected_size=64 * 1024 * 1024,
        manual_override=False,
    )
    state = scheduler._sessions['bulk']
    state.registered_at = time.time() - 60.0
    assert scheduler.try_acquire_request('bulk', 'aged-1') is True
