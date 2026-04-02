"""Phase 7 hardening tests."""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from retiboard.chunks.swarm import SwarmFetcher


def test_swarm_missing_chunk_returns_empty_lists() -> None:
    swarm = SwarmFetcher(
        peer_lxmf_hashes=["a" * 32],
        chunk_count=1,
        next_chunk_timeout=lambda _: 1.0,
        max_inflight_total=1,
        max_inflight_per_peer=1,
    )
    plan = swarm.plan_requests(now=time.time())[0]
    swarm.chunks.pop(plan.chunk_index)
    assert swarm.mark_send_failed(plan) == []
    assert swarm.mark_chunk_stored(plan.request_id) == []
    assert swarm.mark_invalid("missing-request-id") == []


if __name__ == "__main__":
    test_swarm_missing_chunk_returns_empty_lists()
