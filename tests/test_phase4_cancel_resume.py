from retiboard.chunks.swarm import PriorityMode, SwarmFetcher


def test_mark_chunk_stored_returns_duplicate_cancellations():
    swarm = SwarmFetcher(
        peer_lxmf_hashes=["peer-a", "peer-b"],
        chunk_count=2,
        next_chunk_timeout=lambda _idx: 30.0,
        max_inflight_total=2,
        max_inflight_per_peer=1,
        priority_mode=PriorityMode.HYBRID,
    )

    first_round = swarm.plan_requests(now=0.0)
    assert len(first_round) == 2

    primary = next(plan for plan in first_round if plan.chunk_index == 0)
    initial_last = next(plan for plan in first_round if plan.chunk_index == 1)
    swarm.mark_request_sent(primary.request_id)
    swarm.mark_request_sent(initial_last.request_id)

    cancelled = swarm.mark_chunk_stored(primary.request_id)
    assert cancelled == []

    duplicate_round = swarm.plan_requests(now=2.0)
    assert len(duplicate_round) == 1
    duplicate = duplicate_round[0]
    assert duplicate.chunk_index == 1
    assert duplicate.duplicate is True
    swarm.mark_request_sent(duplicate.request_id)

    cancelled = swarm.mark_chunk_stored(initial_last.request_id)
    assert len(cancelled) == 1
    assert cancelled[0].request_id == duplicate.request_id
    assert swarm.is_complete() is True
