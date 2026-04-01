from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .models import ChunkFetchState


class PriorityMode(str, Enum):
    LINEAR = "linear"
    HYBRID = "hybrid"
    RAREST_FIRST = "rarest_first"


@dataclass
class SwarmPeerState:
    peer_lxmf_hash: str
    timeout_count: int = 0
    invalid_chunk_count: int = 0
    success_count: int = 0
    cooldown_until: float = 0.0
    in_flight: int = 0

    def is_available(self, now: float) -> bool:
        return now >= self.cooldown_until


@dataclass
class SwarmChunkState:
    chunk_index: int
    state: ChunkFetchState = ChunkFetchState.MISSING
    attempt_count: int = 0
    stored: bool = False
    active_request_ids: set[str] = field(default_factory=set)
    successful_peer_lxmf_hash: str = ""


@dataclass(frozen=True)
class RequestPlan:
    request_id: str
    peer_lxmf_hash: str
    chunk_index: int
    timeout_seconds: float
    duplicate: bool = False


@dataclass
class ActiveRequest:
    request_id: str
    peer_lxmf_hash: str
    chunk_index: int
    deadline_at: float
    duplicate: bool = False


class SwarmFetcher:
    """Phase 2 multi-peer single-blob scheduler.

    This stage intentionally uses linear / hybrid scheduling only.
    Rarest-first needs peer chunk availability advertisements, which are a
    later phase. For cold start and no availability knowledge, linear is the
    spec-aligned deterministic policy.
    """

    def __init__(
        self,
        *,
        peer_lxmf_hashes: list[str],
        chunk_count: int,
        next_chunk_timeout,
        max_inflight_total: Optional[int] = None,
        max_inflight_per_peer: Optional[int] = None,
        max_attempts_per_chunk: int = 6,
        priority_mode: PriorityMode = PriorityMode.HYBRID,
        peer_chunk_ranges: Optional[dict[str, list[tuple[int, int]]]] = None,
        is_low_bandwidth: bool = False,
    ):
        self.priority_mode = priority_mode

        # Bandwidth-adaptive concurrency limits (§14.4).
        if max_inflight_total is None:
            max_inflight_total = 3 if is_low_bandwidth else 10
        if max_inflight_per_peer is None:
            max_inflight_per_peer = 1 if is_low_bandwidth else 3

        self.max_inflight_total = max(1, max_inflight_total)
        self.max_inflight_per_peer = max(1, max_inflight_per_peer)
        self.max_attempts_per_chunk = max(1, max_attempts_per_chunk)
        self._next_chunk_timeout = next_chunk_timeout
        self.is_low_bandwidth = is_low_bandwidth
        self.peers: dict[str, SwarmPeerState] = {
            peer: SwarmPeerState(peer_lxmf_hash=peer)
            for peer in peer_lxmf_hashes
            if peer
        }
        self.chunks: dict[int, SwarmChunkState] = {
            idx: SwarmChunkState(chunk_index=idx)
            for idx in range(chunk_count)
        }
        self.requests: dict[str, ActiveRequest] = {}
        self.peer_chunk_ranges: dict[str, list[tuple[int, int]]] = {}
        self._recent_timeouts: list[ActiveRequest] = []
        if peer_chunk_ranges:
            for peer, ranges in peer_chunk_ranges.items():
                self.update_peer_availability(peer, ranges)

    def update_peer_availability(self, peer_lxmf_hash: str, ranges: list[tuple[int, int]]) -> None:
        """Record structural chunk availability for one peer."""
        normalized: list[tuple[int, int]] = []
        for start, end in ranges or []:
            start_i = max(0, int(start))
            end_i = min(len(self.chunks) - 1, int(end))
            if end_i < start_i:
                continue
            normalized.append((start_i, end_i))
        normalized.sort()
        self.peer_chunk_ranges[peer_lxmf_hash] = normalized

    def has_peer_availability(self) -> bool:
        return any(ranges for ranges in self.peer_chunk_ranges.values())

    def _peer_has_chunk(self, peer_lxmf_hash: str, chunk_index: int) -> bool:
        ranges = self.peer_chunk_ranges.get(peer_lxmf_hash)
        if not ranges:
            return True
        return any(start <= chunk_index <= end for start, end in ranges)

    def _availability_count(self, chunk_index: int, now: Optional[float] = None) -> int:
        current = time.time() if now is None else now
        count = 0
        for peer in self.peers.values():
            if not peer.is_available(current):
                continue
            if self._peer_has_chunk(peer.peer_lxmf_hash, chunk_index):
                count += 1
        return count



    def apply_persisted_peer_state(
        self,
        peer_lxmf_hash: str,
        *,
        timeout_count: int = 0,
        invalid_chunk_count: int = 0,
        success_count: int = 0,
        cooldown_until: float = 0.0,
    ) -> None:
        peer = self.peers.get(peer_lxmf_hash)
        if peer is None:
            return
        peer.timeout_count = max(peer.timeout_count, int(timeout_count))
        peer.invalid_chunk_count = max(peer.invalid_chunk_count, int(invalid_chunk_count))
        peer.success_count = max(peer.success_count, int(success_count))
        peer.cooldown_until = max(float(peer.cooldown_until), float(cooldown_until))

    def get_peer_state(self, peer_lxmf_hash: str) -> Optional[SwarmPeerState]:
        return self.peers.get(peer_lxmf_hash)

    def progress_snapshot(self) -> dict[str, int | bool]:
        total = len(self.chunks)
        stored = sum(1 for chunk in self.chunks.values() if chunk.stored)
        requested = sum(1 for chunk in self.chunks.values() if chunk.active_request_ids)
        available_peers = sum(1 for peer in self.peers.values() if peer.is_available(time.time()))
        cooled_down_peers = len(self.peers) - available_peers
        percent = int((stored * 100) / total) if total > 0 else 0
        return {
            "chunk_count": total,
            "stored_chunks": stored,
            "requested_chunks": requested,
            "active_requests": len(self.requests),
            "peer_count": len(self.peers),
            "available_peers": available_peers,
            "cooled_down_peers": cooled_down_peers,
            "percent_complete": percent,
            "complete": stored == total and total > 0,
        }

    def active_peer_count(self, now: Optional[float] = None) -> int:
        current = time.time() if now is None else now
        return sum(1 for peer in self.peers.values() if peer.is_available(current))

    def is_complete(self) -> bool:
        return all(chunk.stored for chunk in self.chunks.values())

    def active_request_count(self) -> int:
        return len(self.requests)

    def is_endgame(self) -> bool:
        remaining = sum(1 for chunk in self.chunks.values() if not chunk.stored)
        return remaining <= max(2, self.active_peer_count() * 2)

    def lookup_request(self, request_id: str) -> Optional[ActiveRequest]:
        return self.requests.get(request_id)

    def mark_send_failed(self, plan: RequestPlan) -> list[ActiveRequest]:
        self.requests.pop(plan.request_id, None)
        peer = self.peers.get(plan.peer_lxmf_hash)
        if peer is not None and peer.in_flight > 0:
            peer.in_flight -= 1
        chunk = self.chunks.get(plan.chunk_index)
        if chunk is None:
            return []
        chunk.active_request_ids.discard(plan.request_id)
        if not chunk.stored and not chunk.active_request_ids:
            chunk.state = ChunkFetchState.MISSING
        return []

    def wake_up(self) -> None:
        """Manually trigger a loop iteration (e.g., on path discovery)."""
        pass

    def plan_requests(self, now: Optional[float] = None) -> list[RequestPlan]:
        current = time.time() if now is None else now
        self.process_timeouts(current)

        plans: list[RequestPlan] = []
        if self.is_complete():
            return plans

        peers = [
            peer for peer in sorted(self.peers.values(), key=self._peer_order_key)
            if peer.is_available(current) and peer.in_flight < self.max_inflight_per_peer
        ]
        for peer in peers:
            while peer.in_flight < self.max_inflight_per_peer and len(self.requests) < self.max_inflight_total:
                selected = self._select_chunk_for_peer(peer.peer_lxmf_hash)
                if selected is None:
                    break
                chunk_index, duplicate = selected
                chunk = self.chunks[chunk_index]
                request_id = uuid.uuid4().hex
                timeout_seconds = float(self._next_chunk_timeout(chunk_index))
                plan = RequestPlan(
                    request_id=request_id,
                    peer_lxmf_hash=peer.peer_lxmf_hash,
                    chunk_index=chunk_index,
                    timeout_seconds=timeout_seconds,
                    duplicate=duplicate,
                )
                deadline_at = current + timeout_seconds
                self.requests[request_id] = ActiveRequest(
                    request_id=request_id,
                    peer_lxmf_hash=peer.peer_lxmf_hash,
                    chunk_index=chunk_index,
                    deadline_at=deadline_at,
                    duplicate=duplicate,
                )
                peer.in_flight += 1
                chunk.attempt_count += 1
                chunk.active_request_ids.add(request_id)
                chunk.state = ChunkFetchState.REQUEST_ENQUEUED
                plans.append(plan)
                if chunk.attempt_count >= self.max_attempts_per_chunk and not chunk.stored:
                    # No extra special action here; exhaustion is handled via can_make_progress().
                    pass
        return plans

    def mark_request_sent(self, request_id: str) -> None:
        request = self.requests.get(request_id)
        if request is None:
            return
        chunk = self.chunks.get(request.chunk_index)
        if chunk is not None and not chunk.stored:
            chunk.state = ChunkFetchState.REQUESTED

    def mark_request_deferred(
        self,
        request_id: str,
        *,
        state: ChunkFetchState = ChunkFetchState.REQUEST_ENQUEUED,
    ) -> list[ActiveRequest]:
        request = self.requests.pop(request_id, None)
        if request is None:
            return []
        peer = self.peers.get(request.peer_lxmf_hash)
        if peer is not None and peer.in_flight > 0:
            peer.in_flight -= 1
        chunk = self.chunks.get(request.chunk_index)
        if chunk is None:
            return []
        chunk.active_request_ids.discard(request_id)
        if chunk.attempt_count > 0:
            chunk.attempt_count -= 1
        if chunk.stored:
            return []
        if chunk.active_request_ids:
            chunk.state = ChunkFetchState.REQUESTED
        else:
            chunk.state = state
        return []

    def mark_chunk_stored(self, request_id: str) -> list[ActiveRequest]:
        request = self.requests.pop(request_id, None)
        if request is None:
            return []
        peer = self.peers.get(request.peer_lxmf_hash)
        if peer is not None:
            peer.success_count += 1
            if peer.in_flight > 0:
                peer.in_flight -= 1
        chunk = self.chunks.get(request.chunk_index)
        if chunk is None:
            return []
        chunk.stored = True
        chunk.successful_peer_lxmf_hash = request.peer_lxmf_hash
        chunk.state = ChunkFetchState.STORED
        sibling_ids = list(chunk.active_request_ids)
        chunk.active_request_ids.clear()
        cancelled: list[ActiveRequest] = []
        for sibling_id in sibling_ids:
            if sibling_id == request_id:
                continue
            sibling = self.requests.pop(sibling_id, None)
            if sibling is None:
                continue
            cancelled.append(sibling)
            sibling_peer = self.peers.get(sibling.peer_lxmf_hash)
            if sibling_peer is not None and sibling_peer.in_flight > 0:
                sibling_peer.in_flight -= 1
        return cancelled

    def mark_invalid(self, request_id: str) -> list[ActiveRequest]:
        request = self.requests.pop(request_id, None)
        if request is None:
            return []
        peer = self.peers.get(request.peer_lxmf_hash)
        if peer is not None:
            peer.invalid_chunk_count += 1
            if peer.in_flight > 0:
                peer.in_flight -= 1
            peer.cooldown_until = max(peer.cooldown_until, time.time() + self._invalid_penalty(peer.invalid_chunk_count))
        chunk = self.chunks.get(request.chunk_index)
        if chunk is None:
            return []
        chunk.active_request_ids.discard(request_id)
        if not chunk.stored and not chunk.active_request_ids:
            chunk.state = ChunkFetchState.MISSING
        return []

    def mark_cancelled(self, request_id: str) -> list[ActiveRequest]:
        request = self.requests.pop(request_id, None)
        if request is None:
            return []
        peer = self.peers.get(request.peer_lxmf_hash)
        if peer is not None and peer.in_flight > 0:
            peer.in_flight -= 1
        chunk = self.chunks.get(request.chunk_index)
        if chunk is None:
            return []
        chunk.active_request_ids.discard(request_id)
        if chunk.stored:
            return []
        if chunk.active_request_ids:
            chunk.state = ChunkFetchState.REQUESTED
        else:
            chunk.state = ChunkFetchState.CANCELLED
        return []

    def process_timeouts(self, now: Optional[float] = None) -> int:
        current = time.time() if now is None else now
        expired = [request_id for request_id, request in self.requests.items() if request.deadline_at <= current]
        self._recent_timeouts = []
        for request_id in expired:
            request = self.requests.pop(request_id, None)
            if request is None:
                continue
            self._recent_timeouts.append(request)
            peer = self.peers.get(request.peer_lxmf_hash)
            if peer is not None:
                peer.timeout_count += 1
                if peer.in_flight > 0:
                    peer.in_flight -= 1
                peer.cooldown_until = max(peer.cooldown_until, current + self._timeout_penalty(peer.timeout_count))
            chunk = self.chunks.get(request.chunk_index)
            if chunk is not None:
                chunk.active_request_ids.discard(request_id)
                if not chunk.stored and not chunk.active_request_ids:
                    chunk.state = ChunkFetchState.MISSING
        return len(expired)

    def take_recent_timeouts(self) -> list[ActiveRequest]:
        expired = list(self._recent_timeouts)
        self._recent_timeouts.clear()
        return expired

    def can_make_progress(self, now: Optional[float] = None) -> bool:
        if self.is_complete() or self.requests:
            return True
        
        # If any chunks are still missing/failed and haven't hit their max attempts,
        # we can still make progress once peers come off cooldown.
        for chunk in self.chunks.values():
            if not chunk.stored and chunk.attempt_count < self.max_attempts_per_chunk:
                return True
        
        return False

    def _select_chunk_for_peer(self, peer_lxmf_hash: str) -> Optional[tuple[int, bool]]:
        for chunk in self._ordered_chunks(peer_lxmf_hash):
            if chunk.stored:
                continue
            if chunk.attempt_count >= self.max_attempts_per_chunk:
                continue
            if not self._peer_has_chunk(peer_lxmf_hash, chunk.chunk_index):
                continue
            if not chunk.active_request_ids:
                return chunk.chunk_index, False

        if not self.is_endgame():
            return None

        for chunk in self._ordered_chunks(peer_lxmf_hash):
            if chunk.stored:
                continue
            if chunk.attempt_count >= self.max_attempts_per_chunk:
                continue
            if not self._peer_has_chunk(peer_lxmf_hash, chunk.chunk_index):
                continue
            if len(chunk.active_request_ids) != 1:
                continue
            active_request = next(iter(chunk.active_request_ids))
            request = self.requests.get(active_request)
            if request is None or request.peer_lxmf_hash == peer_lxmf_hash:
                continue
            return chunk.chunk_index, True
        return None

    def _ordered_chunks(self, peer_lxmf_hash: Optional[str] = None) -> list[SwarmChunkState]:
        if self.priority_mode == PriorityMode.RAREST_FIRST and self.has_peer_availability():
            ordered_indexes = sorted(
                self.chunks.keys(),
                key=lambda idx: (self._availability_count(idx), idx),
            )
            return [self.chunks[idx] for idx in ordered_indexes]
        return [self.chunks[idx] for idx in sorted(self.chunks.keys())]

    @staticmethod
    def _peer_order_key(peer: SwarmPeerState) -> tuple:
        return (
            -peer.success_count,
            peer.timeout_count + peer.invalid_chunk_count,
            peer.in_flight,
            peer.peer_lxmf_hash,
        )

    @staticmethod
    def _timeout_penalty(timeout_count: int) -> float:
        exponent = max(0, timeout_count - 1)
        return min(180.0, 10.0 * (2 ** exponent))

    @staticmethod
    def _invalid_penalty(invalid_count: int) -> float:
        exponent = max(0, invalid_count - 1)
        return min(600.0, 60.0 * (2 ** exponent))
