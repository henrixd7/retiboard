"""
Per-peer message queue for LXMF delivery.

v3.6.2 §8 — Message Queue (NORMATIVE, Phase 5 prerequisite).

Each node MUST implement a per-peer message queue with:
  §8.1 — Two priority tiers: control (DELTA_REQUEST/RESPONSE) and data (PAYLOAD).
          Within a tier: FIFO. Between tiers: control always dequeued first.
          Per-message TTL (300s control, 3600s data). Max 32 msgs/peer.
          Global memory ceiling. TTL eviction on enqueue + periodic sweep.
  §8.2 — Flush: on path discovery, dequeue control-first then data.
          If delivery fails mid-flush, remaining stay queued + backoff.
  §8.3 — Persistence: OPTIONAL (not implemented in v1).

Security (§11.3):
  Messages for peers that exceeded max_path_retries MUST be rejected
  immediately rather than queued.
"""

from __future__ import annotations

import time
import threading
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Optional

import RNS


# =========================================================================
# Constants (v3.6.2 Appendix A: [queue] section)
# =========================================================================

QUEUE_MAX_DEPTH_PER_PEER = 32
QUEUE_CONTROL_TTL = 300        # 5 minutes
QUEUE_DATA_TTL = 3600          # 1 hour
QUEUE_MAX_TOTAL_MEMORY_MB = 16
QUEUE_EVICTION_SWEEP_INTERVAL = 60  # seconds


class Priority(IntEnum):
    """Two-tier priority (§8.1). Control is always dequeued first."""
    CONTROL = 0   # DELTA_REQUEST, DELTA_RESPONSE, HAVE
    DATA = 1      # PAYLOAD, METADATA


class SendResult(Enum):
    """Outcome of a send_lxmf() call.

    Truthiness: SENT and QUEUED are truthy (message accepted),
    REJECTED is falsy (message dropped). This preserves backward
    compatibility with callers that do `if ok:`.
    """
    SENT = "sent"
    QUEUED = "queued"
    REJECTED = "rejected"

    def __bool__(self) -> bool:
        return self is not SendResult.REJECTED


@dataclass
class QueuedMessage:
    """A single message waiting for delivery to a peer."""
    peer_lxmf_hash: str
    payload: bytes          # Serialized LXMF-ready content
    priority: Priority
    title: str              # LXMF message type tag
    ttl: float              # Seconds until expiry
    enqueued_at: float = field(default_factory=time.time)
    size_bytes: int = 0     # Cached for global memory accounting

    def __post_init__(self):
        self.size_bytes = len(self.payload) + 128  # payload + overhead estimate

    @property
    def expired(self) -> bool:
        return (time.time() - self.enqueued_at) > self.ttl


class PeerQueue:
    """
    Per-peer message queue with two priority tiers (§8.1).

    Keyed by peer_lxmf_hash. FIFO within each tier.
    Control tier is always dequeued before data tier.
    """

    def __init__(self, peer_lxmf_hash: str, max_depth: int = QUEUE_MAX_DEPTH_PER_PEER):
        self.peer_lxmf_hash = peer_lxmf_hash
        self.max_depth = max_depth
        self._control: deque[QueuedMessage] = deque()
        self._data: deque[QueuedMessage] = deque()

    @property
    def depth(self) -> int:
        return len(self._control) + len(self._data)

    @property
    def total_bytes(self) -> int:
        return (
            sum(m.size_bytes for m in self._control)
            + sum(m.size_bytes for m in self._data)
        )

    @property
    def empty(self) -> bool:
        return self.depth == 0

    def enqueue(self, msg: QueuedMessage) -> bool:
        """
        Enqueue a message. Returns True on success.

        §8.1: On depth overflow, evict oldest data messages first.
        Control messages evicted only if no data remains.
        TTL expiry checked on enqueue.
        """
        # Purge expired messages first.
        self._sweep_expired()

        # Depth overflow → evict.
        while self.depth >= self.max_depth:
            if self._data:
                evicted = self._data.popleft()
                RNS.log(
                    f"Queue overflow for {self.peer_lxmf_hash[:16]}: "
                    f"evicted data msg (age {time.time() - evicted.enqueued_at:.0f}s)",
                    RNS.LOG_DEBUG,
                )
            elif self._control:
                evicted = self._control.popleft()
                RNS.log(
                    f"Queue overflow for {self.peer_lxmf_hash[:16]}: "
                    f"evicted control msg (age {time.time() - evicted.enqueued_at:.0f}s)",
                    RNS.LOG_DEBUG,
                )
            else:
                break  # Shouldn't happen

        if msg.priority == Priority.CONTROL:
            self._control.append(msg)
        else:
            self._data.append(msg)
        return True

    def dequeue(self) -> Optional[QueuedMessage]:
        """
        Dequeue next message: control-first, then data (§8.1).

        Skips and discards expired messages.
        """
        while self._control:
            msg = self._control.popleft()
            if not msg.expired:
                return msg
        while self._data:
            msg = self._data.popleft()
            if not msg.expired:
                return msg
        return None

    def drain_all(self) -> list[QueuedMessage]:
        """
        Drain all non-expired messages in priority order for flush (§8.2).

        Returns list: all control messages first, then all data messages.
        """
        result = []
        while self._control:
            msg = self._control.popleft()
            if not msg.expired:
                result.append(msg)
        while self._data:
            msg = self._data.popleft()
            if not msg.expired:
                result.append(msg)
        return result

    def requeue_batch(self, messages: list[QueuedMessage]) -> None:
        """
        Re-enqueue a batch of messages that failed mid-flush (§8.2).

        Preserves their original enqueued_at for TTL accounting.
        Inserts at the FRONT of the appropriate tier.
        """
        for msg in reversed(messages):
            if msg.expired:
                continue
            if msg.priority == Priority.CONTROL:
                self._control.appendleft(msg)
            else:
                self._data.appendleft(msg)

    def _sweep_expired(self) -> int:
        """Remove expired messages from both tiers. Returns count removed."""
        removed = 0
        for tier in (self._control, self._data):
            before = len(tier)
            # Rebuild without expired entries.
            fresh = deque(m for m in tier if not m.expired)
            removed += before - len(fresh)
            tier.clear()
            tier.extend(fresh)
        return removed


class MessageQueueManager:
    """
    Global message queue manager (v3.6.2 §8).

    Manages per-peer queues with a global memory ceiling.
    Provides the queue/flush/sweep interface used by the sync engine.
    """

    def __init__(
        self,
        max_depth_per_peer: int = QUEUE_MAX_DEPTH_PER_PEER,
        max_total_memory_mb: int = QUEUE_MAX_TOTAL_MEMORY_MB,
    ):
        self._lock = threading.Lock()
        self._queues: dict[str, PeerQueue] = {}
        self._max_depth = max_depth_per_peer
        self._max_total_bytes = max_total_memory_mb * 1024 * 1024

    def queue_message(
        self,
        peer_lxmf_hash: str,
        payload: bytes,
        title: str,
        priority: Priority = Priority.DATA,
        ttl: Optional[float] = None,
    ) -> bool:
        """
        Queue a message for a peer (§8.1).

        Args:
            peer_lxmf_hash: Target peer's LXMF delivery hash.
            payload: Serialized message content (UTF-8 bytes).
            title: LXMF message type tag.
            priority: CONTROL or DATA tier.
            ttl: Per-message TTL in seconds. Defaults per priority tier.

        Returns:
            True if queued, False if rejected (e.g., global ceiling hit).
        """
        if ttl is None:
            ttl = QUEUE_CONTROL_TTL if priority == Priority.CONTROL else QUEUE_DATA_TTL

        msg = QueuedMessage(
            peer_lxmf_hash=peer_lxmf_hash,
            payload=payload,
            priority=priority,
            title=title,
            ttl=ttl,
        )

        with self._lock:
            # Global memory ceiling check (§8.1).
            if self._total_bytes() + msg.size_bytes > self._max_total_bytes:
                RNS.log(
                    f"Global queue memory ceiling reached "
                    f"({self._max_total_bytes // (1024*1024)} MB), "
                    f"rejecting message for {peer_lxmf_hash[:16]}",
                    RNS.LOG_WARNING,
                )
                return False

            if peer_lxmf_hash not in self._queues:
                self._queues[peer_lxmf_hash] = PeerQueue(
                    peer_lxmf_hash, self._max_depth,
                )

            return self._queues[peer_lxmf_hash].enqueue(msg)

    def flush_peer(self, peer_lxmf_hash: str) -> list[QueuedMessage]:
        """
        Drain all queued messages for a peer in priority order (§8.2).

        Called when a path is discovered or on periodic retry.
        Returns messages for the caller to attempt delivery.
        """
        with self._lock:
            pq = self._queues.get(peer_lxmf_hash)
            if pq is None or pq.empty:
                return []
            messages = pq.drain_all()
            if pq.empty:
                del self._queues[peer_lxmf_hash]
            return messages

    def requeue_failed(
        self, peer_lxmf_hash: str, messages: list[QueuedMessage],
    ) -> None:
        """Re-enqueue messages that failed delivery mid-flush (§8.2)."""
        with self._lock:
            if peer_lxmf_hash not in self._queues:
                self._queues[peer_lxmf_hash] = PeerQueue(
                    peer_lxmf_hash, self._max_depth,
                )
            self._queues[peer_lxmf_hash].requeue_batch(messages)

    def has_queued(self, peer_lxmf_hash: str) -> bool:
        """Check if a peer has any queued messages."""
        with self._lock:
            pq = self._queues.get(peer_lxmf_hash)
            return pq is not None and not pq.empty

    def sweep_expired(self) -> int:
        """
        Periodic TTL eviction sweep across all queues (§8.1).

        Called by the engine's background loop every EVICTION_SWEEP_INTERVAL.
        Returns total messages evicted.
        """
        total = 0
        with self._lock:
            dead_peers = []
            for peer_hash, pq in self._queues.items():
                total += pq._sweep_expired()
                if pq.empty:
                    dead_peers.append(peer_hash)
            for ph in dead_peers:
                del self._queues[ph]
        if total > 0:
            RNS.log(f"Queue sweep: evicted {total} expired message(s)", RNS.LOG_DEBUG)
        return total

    def peer_queue_depth(self, peer_lxmf_hash: str) -> int:
        """Queue depth for a specific peer."""
        with self._lock:
            pq = self._queues.get(peer_lxmf_hash)
            return pq.depth if pq else 0

    def total_depth(self) -> int:
        """Total messages queued across all peers."""
        with self._lock:
            return sum(pq.depth for pq in self._queues.values())

    def _total_bytes(self) -> int:
        """Total estimated memory usage across all queues."""
        return sum(pq.total_bytes for pq in self._queues.values())
