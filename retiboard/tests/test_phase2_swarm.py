"""Phase 2 swarm scheduler tests.

Covers:
    1. Multi-peer request planning
    2. Timeout cooldown and retry eligibility
    3. Limited endgame duplication
    4. Invalid chunk penalties
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from retiboard.chunks.models import ChunkFetchState
from retiboard.chunks.swarm import SwarmFetcher


def test_basic_planning() -> None:
    swarm = SwarmFetcher(
        peer_lxmf_hashes=["a" * 32, "b" * 32, "c" * 32],
        chunk_count=6,
        next_chunk_timeout=lambda _: 30.0,
        max_inflight_total=3,
        max_inflight_per_peer=1,
    )
    plans = swarm.plan_requests(now=time.time())
    assert len(plans) == 3
    assert sorted(plan.chunk_index for plan in plans) == [0, 1, 2]
    print("  [01] basic planning PASS")


def test_timeout_cooldown_and_retry() -> None:
    swarm = SwarmFetcher(
        peer_lxmf_hashes=["a" * 32, "b" * 32],
        chunk_count=3,
        next_chunk_timeout=lambda _: 0.01,
        max_inflight_total=2,
        max_inflight_per_peer=1,
    )
    plans = swarm.plan_requests(now=time.time())
    for plan in plans:
        swarm.mark_request_sent(plan.request_id)
    time.sleep(0.02)
    expired = swarm.process_timeouts(now=time.time())
    assert expired == 2
    assert swarm.can_make_progress(now=time.time() + 30.0) is True
    print("  [02] timeout cooldown PASS")


def test_endgame_duplication() -> None:
    swarm = SwarmFetcher(
        peer_lxmf_hashes=["a" * 32, "b" * 32],
        chunk_count=2,
        next_chunk_timeout=lambda _: 30.0,
        max_inflight_total=2,
        max_inflight_per_peer=1,
    )
    first = swarm.plan_requests(now=time.time())
    assert len(first) == 2
    swarm.mark_chunk_stored(first[0].request_id)
    # Leave one chunk in flight, then ask for more work in endgame.
    second = swarm.plan_requests(now=time.time())
    assert len(second) <= 1
    if second:
        assert second[0].duplicate is True
    print("  [03] endgame duplication PASS")


def test_invalid_penalty() -> None:
    swarm = SwarmFetcher(
        peer_lxmf_hashes=["a" * 32, "b" * 32],
        chunk_count=2,
        next_chunk_timeout=lambda _: 30.0,
        max_inflight_total=1,
        max_inflight_per_peer=1,
    )
    plans = swarm.plan_requests(now=time.time())
    assert len(plans) == 1
    request_id = plans[0].request_id
    peer_hash = plans[0].peer_lxmf_hash
    swarm.mark_invalid(request_id)
    assert swarm.peers[peer_hash].invalid_chunk_count == 1
    assert swarm.peers[peer_hash].cooldown_until > time.time()
    print("  [04] invalid penalty PASS")


def test_cancelled_request_releases_inflight_state() -> None:
    swarm = SwarmFetcher(
        peer_lxmf_hashes=["a" * 32],
        chunk_count=1,
        next_chunk_timeout=lambda _: 30.0,
        max_inflight_total=1,
        max_inflight_per_peer=1,
    )
    plan = swarm.plan_requests(now=time.time())[0]
    swarm.mark_request_sent(plan.request_id)
    swarm.mark_cancelled(plan.request_id)
    assert swarm.active_request_count() == 0
    assert swarm.peers["a" * 32].in_flight == 0
    assert swarm.chunks[0].active_request_ids == set()
    assert swarm.chunks[0].state == ChunkFetchState.CANCELLED
    print("  [05] cancel release PASS")


def test_deferred_request_rolls_back_attempt_without_holding_slot() -> None:
    swarm = SwarmFetcher(
        peer_lxmf_hashes=["a" * 32],
        chunk_count=1,
        next_chunk_timeout=lambda _: 30.0,
        max_inflight_total=1,
        max_inflight_per_peer=1,
    )
    plan = swarm.plan_requests(now=time.time())[0]
    assert swarm.chunks[0].attempt_count == 1
    swarm.mark_request_deferred(plan.request_id)
    assert swarm.active_request_count() == 0
    assert swarm.peers["a" * 32].in_flight == 0
    assert swarm.chunks[0].attempt_count == 0
    assert swarm.chunks[0].state == ChunkFetchState.REQUEST_ENQUEUED
    print("  [06] deferred request PASS")


if __name__ == "__main__":
    test_basic_planning()
    test_timeout_cooldown_and_retry()
    test_endgame_duplication()
    test_invalid_penalty()
    test_cancelled_request_releases_inflight_state()
    test_deferred_request_rolls_back_attempt_without_holding_slot()
