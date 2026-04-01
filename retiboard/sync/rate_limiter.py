"""
Rate limiting and backpressure for RetiBoard gossip.

Spec references:
    §7.1 — "Maximum 5 concurrent thread syncs per board."
           "Lower limits (max 2) on LoRa/slow interfaces."
           "Exponential backoff + jitter on failures."

Provides:
  - Per-board concurrent sync semaphores
  - Exponential backoff with jitter for failed operations
  - LoRa-aware limit adjustment
"""

from __future__ import annotations

import asyncio
import random
import time

import RNS

from retiboard.config import MAX_CONCURRENT_SYNCS, MAX_CONCURRENT_SYNCS_LORA


class SyncRateLimiter:
    """
    Per-board rate limiter for gossip operations.

    Limits concurrent thread syncs to MAX_CONCURRENT_SYNCS (5 normal, 2 LoRa).
    Provides exponential backoff tracking for failed peers/operations.
    """

    def __init__(self, is_low_bandwidth: bool = False):
        """
        Args:
            is_low_bandwidth: True if on LoRa/slow interface (§8.4).
        """
        self._is_low_bandwidth = is_low_bandwidth
        self._max_concurrent = (
            MAX_CONCURRENT_SYNCS_LORA if is_low_bandwidth
            else MAX_CONCURRENT_SYNCS
        )

        # Per-board semaphores: {board_id: asyncio.Semaphore}
        self._semaphores: dict[str, asyncio.Semaphore] = {}

        # Backoff tracking: {(board_id, peer_hex): next_allowed_time}
        self._backoff: dict[tuple[str, str], float] = {}

        # Failure counts for backoff calculation: {key: consecutive_failures}
        self._failures: dict[tuple[str, str], int] = {}

    def _get_semaphore(self, board_id: str) -> asyncio.Semaphore:
        """Get or create the semaphore for a board."""
        if board_id not in self._semaphores:
            self._semaphores[board_id] = asyncio.Semaphore(self._max_concurrent)
        return self._semaphores[board_id]

    async def acquire(self, board_id: str) -> bool:
        """
        Acquire a sync slot for a board.

        Returns True if acquired, blocks until available.
        Use as: await limiter.acquire(board_id)
        Always pair with release().
        """
        sem = self._get_semaphore(board_id)
        await sem.acquire()
        return True

    def release(self, board_id: str) -> None:
        """Release a sync slot for a board."""
        sem = self._get_semaphore(board_id)
        sem.release()

    def can_sync_peer(self, board_id: str, peer_hex: str) -> bool:
        """
        Check if we should attempt sync with a peer (backoff check).

        Returns False if the peer is in backoff period.
        """
        key = (board_id, peer_hex)
        next_allowed = self._backoff.get(key, 0)
        return time.time() >= next_allowed

    def record_failure(self, board_id: str, peer_hex: str) -> None:
        """
        Record a sync failure for backoff calculation.

        Exponential backoff: base=5s, factor=2, max=300s, with jitter.
        """
        key = (board_id, peer_hex)
        failures = self._failures.get(key, 0) + 1
        self._failures[key] = failures

        # Exponential backoff: 5, 10, 20, 40, 80, 160, 300 (capped)
        base_delay = 5.0
        delay = min(base_delay * (2 ** (failures - 1)), 300.0)
        # Add jitter: ±25%
        jitter = delay * 0.25 * (2 * random.random() - 1)
        actual_delay = delay + jitter

        self._backoff[key] = time.time() + actual_delay

        RNS.log(
            f"Sync backoff for {peer_hex[:16]} on board {board_id[:8]}: "
            f"{actual_delay:.1f}s (failure #{failures})",
            RNS.LOG_DEBUG,
        )

    def record_success(self, board_id: str, peer_hex: str) -> None:
        """Reset backoff on successful sync."""
        key = (board_id, peer_hex)
        self._failures.pop(key, None)
        self._backoff.pop(key, None)

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent
