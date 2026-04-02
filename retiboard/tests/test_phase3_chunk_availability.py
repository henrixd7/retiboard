"""Phase 3 chunk availability / rarest-first scheduler tests."""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from retiboard.chunks.swarm import PriorityMode, SwarmFetcher


def test_rarest_first_prefers_scarce_chunks() -> None:
    swarm = SwarmFetcher(
        peer_lxmf_hashes=["a" * 32, "b" * 32, "c" * 32],
        chunk_count=4,
        next_chunk_timeout=lambda _: 30.0,
        max_inflight_total=2,
        max_inflight_per_peer=1,
        priority_mode=PriorityMode.RAREST_FIRST,
        peer_chunk_ranges={
            "a" * 32: [(0, 3)],
            "b" * 32: [(0, 1)],
            "c" * 32: [(0, 0)],
        },
    )
    plans = swarm.plan_requests(now=time.time())
    assert len(plans) == 2
    planned = sorted(plan.chunk_index for plan in plans)
    assert planned == [1, 2]
    print("  [01] rarest-first scarcity PASS")


def test_rarest_first_respects_peer_ranges() -> None:
    swarm = SwarmFetcher(
        peer_lxmf_hashes=["a" * 32, "b" * 32],
        chunk_count=3,
        next_chunk_timeout=lambda _: 30.0,
        max_inflight_total=2,
        max_inflight_per_peer=1,
        priority_mode=PriorityMode.RAREST_FIRST,
        peer_chunk_ranges={
            "a" * 32: [(0, 0)],
            "b" * 32: [(1, 2)],
        },
    )
    plans = swarm.plan_requests(now=time.time())
    assert len(plans) == 2
    assigned = {plan.peer_lxmf_hash: plan.chunk_index for plan in plans}
    assert assigned["a" * 32] == 0
    assert assigned["b" * 32] == 1
    print("  [02] peer range respect PASS")


if __name__ == "__main__":
    test_rarest_first_prefers_scarce_chunks()
    test_rarest_first_respects_peer_ranges()
