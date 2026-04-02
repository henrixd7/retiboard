# ruff: noqa: E402

"""Task 4 session-lock hardening tests."""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
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

from retiboard.chunks.swarm import PriorityMode, SwarmFetcher
from retiboard.sync.payload_fetch import _build_live_progress_snapshot, register_pending_chunk_session


def test_live_progress_snapshot_is_safe_during_swarm_mutation() -> None:
    loop = asyncio.new_event_loop()
    try:
        session = register_pending_chunk_session(
            "board-lock-test",
            "a" * 64,
            "attachments",
            "peer-a",
            loop,
        )
        with session.lock:
            session.candidate_peer_lxmf_hashes = ["peer-a", "peer-b", "peer-c"]
            session.swarm = SwarmFetcher(
                peer_lxmf_hashes=session.candidate_peer_lxmf_hashes,
                chunk_count=32,
                next_chunk_timeout=lambda _idx: 0.01,
                max_inflight_total=3,
                max_inflight_per_peer=1,
                priority_mode=PriorityMode.HYBRID,
            )

        stop = threading.Event()
        errors: list[BaseException] = []

        def mutator() -> None:
            try:
                while not stop.is_set():
                    with session.lock:
                        plans = session.swarm.plan_requests(now=time.time())
                        for plan in plans:
                            session.swarm.mark_request_sent(plan.request_id)
                        for plan in plans:
                            session.swarm.mark_send_failed(plan)
            except BaseException as exc:  # pragma: no cover - test capture
                errors.append(exc)
                stop.set()

        def snapshotter() -> None:
            try:
                for _ in range(500):
                    _build_live_progress_snapshot(session)
                    if stop.is_set():
                        break
            except BaseException as exc:  # pragma: no cover - test capture
                errors.append(exc)
                stop.set()
            finally:
                stop.set()

        t1 = threading.Thread(target=mutator, daemon=True)
        t2 = threading.Thread(target=snapshotter, daemon=True)
        t1.start()
        t2.start()
        t2.join(timeout=5)
        stop.set()
        t1.join(timeout=5)

        assert not errors, f"unexpected concurrent session access error(s): {errors!r}"
    finally:
        loop.close()
