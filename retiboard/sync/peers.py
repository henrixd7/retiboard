"""
Peer tracking for RetiBoard v3.6.2.

Redesigned per the v3.6.2 Routing Specification:
  - Peer-centric: primary key is peer_lxmf_hash (not board_id)
  - Path state machine: unknown → requested → known → stale → unreachable
  - Board index: secondary lookup from board_id → set(peer_lxmf_hash)
  - Trust model: announce hashes are advisory; message.source is authoritative
  - Configurable limits: max peer table size, eviction policy

§5.1 Data Model:
  peer_table keyed by peer_lxmf_hash
  board_index keyed by board_id → set(peer_lxmf_hash)
"""

from __future__ import annotations

import json
import random
import time
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


import RNS

from retiboard.config import REPLICATION_FANOUT, PAYLOAD_FETCH_PEERS


class PathState(Enum):
    UNKNOWN = "unknown"
    REQUESTED = "requested"
    KNOWN = "known"
    STALE = "stale"
    UNREACHABLE = "unreachable"


PATH_REQUEST_TIMEOUT = 30
PATH_KNOWN_TTL = 600
MAX_PATH_RETRIES = 5
RETRY_BACKOFF_BASE = 2.0
RETRY_BACKOFF_MAX = 600
PEER_UNREACHABLE_TTL = 3600
PEER_EXPIRY_SECONDS = 900
MAX_PEER_TABLE_SIZE = 200


@dataclass
class PeerInfo:
    """Peer keyed by lxmf_hash — the ONLY valid LXMF delivery target (§2.1)."""
    lxmf_hash: str
    identity: Optional[RNS.Identity] = None
    boards: set = field(default_factory=set)
    last_seen: float = field(default_factory=time.time)
    path_state: PathState = PathState.UNKNOWN
    verified: bool = False
    announce_hash: Optional[str] = None
    retry_count: int = 0
    next_retry_at: float = 0.0
    delivery_successes: int = 0
    delivery_failures: int = 0
    last_success_at: float = 0.0
    last_failure_at: float = 0.0
    last_direct_have_at: float = 0.0

    @property
    def hexhash(self) -> str:
        return self.lxmf_hash

    @property
    def destination_hash(self) -> bytes:
        return bytes.fromhex(self.lxmf_hash)

    def touch(self):
        self.last_seen = time.time()

    def is_expired(self, now=None):
        if now is None:
            now = time.time()
        return (now - self.last_seen) > PEER_EXPIRY_SECONDS

    def next_retry_delay(self):
        delay = min(PATH_REQUEST_TIMEOUT * (RETRY_BACKOFF_BASE ** self.retry_count), RETRY_BACKOFF_MAX)
        return delay + random.uniform(0, delay * 0.25)

    def to_dict(self) -> dict:
        """Serialize peer info for disk persistence."""
        return {
            "lxmf_hash": self.lxmf_hash,
            "identity": self.identity.get_public_key().hex() if self.identity else None,
            "boards": list(self.boards),
            "last_seen": self.last_seen,
            "path_state": self.path_state.value,
            "verified": self.verified,
            "announce_hash": self.announce_hash,
            "delivery_successes": self.delivery_successes,
            "delivery_failures": self.delivery_failures,
            "last_success_at": self.last_success_at,
            "last_failure_at": self.last_failure_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> PeerInfo:
        """Reconstruct peer info from dict."""
        identity = None
        if data.get("identity"):
            try:
                # Correctly reconstruct from public key bytes.
                identity = RNS.Identity.from_bytes(bytes.fromhex(data["identity"]))
            except Exception:
                pass

        return cls(
            lxmf_hash=data["lxmf_hash"],
            identity=identity,
            boards=set(data.get("boards", [])),
            last_seen=data.get("last_seen", time.time()),
            path_state=PathState(data.get("path_state", "unknown")),
            verified=data.get("verified", False),
            announce_hash=data.get("announce_hash"),
            delivery_successes=data.get("delivery_successes", 0),
            delivery_failures=data.get("delivery_failures", 0),
            last_success_at=data.get("last_success_at", 0.0),
            last_failure_at=data.get("last_failure_at", 0.0),
        )


class PeerTracker:
    """v3.6.2 peer-centric tracker. Primary key: peer_lxmf_hash."""

    def __init__(self):
        self._lock = threading.Lock()
        self._peers: dict[str, PeerInfo] = {}
        self._board_index: dict[str, set[str]] = {}
        self._self_lxmf_hash: str = ""

    def set_self_hash(self, lxmf_hash: str) -> None:
        with self._lock:
            self._self_lxmf_hash = lxmf_hash or ""

    def persist(self, filepath: Path) -> bool:
        """Save peer table to disk."""
        with self._lock:
            # v3.6.3: Only persist verified peers seen in the last 15 mins.
            # This ensures we don't load 'phantom' identities that no longer exist.
            now = time.time()
            data = {
                h: p.to_dict()
                for h, p in self._peers.items()
                if p.verified and (now - p.last_seen < 900)
            }

        try:
            temp_path = filepath.with_suffix(".tmp")
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            temp_path.replace(filepath)
            RNS.log(f"Persisted {len(data)} peer(s) to {filepath}", RNS.LOG_DEBUG)
            return True
        except Exception as e:
            RNS.log(f"Failed to persist peers to {filepath}: {e}", RNS.LOG_ERROR)
            return False

    def load(self, filepath: Path) -> int:
        """Load peer table from disk."""
        if not filepath.exists():
            return 0

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)

            count = 0
            with self._lock:
                for h, p_dict in data.items():
                    if self._is_self(h):
                        continue
                    if h not in self._peers:
                        p = PeerInfo.from_dict(p_dict)
                        self._peers[h] = p
                        for bid in p.boards:
                            self._board_index.setdefault(bid, set()).add(h)
                        count += 1
            RNS.log(f"Loaded {count} peer(s) from {filepath}", RNS.LOG_INFO)
            return count
        except Exception as e:
            RNS.log(f"Failed to load peers from {filepath}: {e}", RNS.LOG_ERROR)
            return 0

    def get_pex_peers(self, board_id: str, count: int = 3) -> list[dict]:
        """Return a random sample of active, verified peers for PEX (§8.2)."""
        now = time.time()
        with self._lock:
            # Filter for peers that participate in this board and are verified.
            hashes = list(self._board_index.get(board_id, set()))
            candidates = [
                self._peers[h] for h in hashes
                if h in self._peers
                and not self._is_self(h)
                and self._peers[h].verified
                and self._peers[h].identity is not None
                and (now - self._peers[h].last_seen < 7200) # seen in last 2h
            ]

        if not candidates:
            return []

        sample = random.sample(candidates, min(len(candidates), count))
        return [
            {
                "h": p.lxmf_hash,
                "i": p.identity.get_public_key().hex() if p.identity else None
            }
            for p in sample
        ]

    def _is_self(self, lxmf_hash: Optional[str]) -> bool:
        return bool(lxmf_hash) and lxmf_hash == self._self_lxmf_hash

    @staticmethod
    def _is_valid_peer_hash(peer_lxmf_hash: Optional[str]) -> bool:
        if not isinstance(peer_lxmf_hash, str) or len(peer_lxmf_hash) not in (16, 32):
            return False
        try:
            bytes.fromhex(peer_lxmf_hash)
        except ValueError:
            return False
        return True

    def register_peer_identity(self, peer_lxmf_hash, identity=None, announce_hash=None):
        """Register a discovered peer without asserting board membership."""
        if not self._is_valid_peer_hash(peer_lxmf_hash):
            RNS.log(f"Rejected malformed peer hash: {peer_lxmf_hash!r}", RNS.LOG_DEBUG)
            return False
        if self._is_self(peer_lxmf_hash):
            return False
        with self._lock:
            self._enforce_limit()
            if peer_lxmf_hash in self._peers:
                p = self._peers[peer_lxmf_hash]
                p.touch()
                if identity:
                    p.identity = identity
                if announce_hash:
                    p.announce_hash = announce_hash
            else:
                self._peers[peer_lxmf_hash] = PeerInfo(
                    lxmf_hash=peer_lxmf_hash, identity=identity,
                    verified=False, announce_hash=announce_hash,
                )
        return True

    def register_from_announce(self, board_id, peer_lxmf_hash, identity=None, announce_hash=None):
        """Register peer from board announce (§5.2). Advisory until verified."""
        if not self._is_valid_peer_hash(peer_lxmf_hash):
            RNS.log(f"Rejected malformed peer hash from announce: {peer_lxmf_hash!r}", RNS.LOG_DEBUG)
            return False
        if self._is_self(peer_lxmf_hash):
            return False
        with self._lock:
            self._enforce_limit()
            if peer_lxmf_hash in self._peers:
                p = self._peers[peer_lxmf_hash]
                p.touch()
                p.boards.add(board_id)
                if identity:
                    p.identity = identity
                if announce_hash:
                    p.announce_hash = announce_hash
            else:
                self._peers[peer_lxmf_hash] = PeerInfo(
                    lxmf_hash=peer_lxmf_hash, identity=identity,
                    boards={board_id}, verified=False, announce_hash=announce_hash,
                )
            self._board_index.setdefault(board_id, set()).add(peer_lxmf_hash)
        RNS.log(f"Peer from announce: {peer_lxmf_hash[:16]} board {board_id[:8]}", RNS.LOG_DEBUG)
        return True

    def register_from_message(self, source_lxmf_hash, board_id=None, identity=None):
        """Register/upgrade peer from LXMF message.source (§5.2). Authoritative."""
        if not self._is_valid_peer_hash(source_lxmf_hash):
            RNS.log(f"Rejected malformed peer hash from message: {source_lxmf_hash!r}", RNS.LOG_DEBUG)
            return False
        if self._is_self(source_lxmf_hash):
            return False
        with self._lock:
            self._enforce_limit()
            if source_lxmf_hash in self._peers:
                p = self._peers[source_lxmf_hash]
                p.touch()
                p.verified = True
                p.path_state = PathState.KNOWN
                p.retry_count = 0
                if identity:
                    p.identity = identity
                if board_id:
                    p.boards.add(board_id)
            else:
                self._peers[source_lxmf_hash] = PeerInfo(
                    lxmf_hash=source_lxmf_hash, identity=identity,
                    boards={board_id} if board_id else set(),
                    verified=True, path_state=PathState.KNOWN,
                )
            if board_id:
                self._board_index.setdefault(board_id, set()).add(source_lxmf_hash)
        RNS.log(f"Peer verified from msg: {source_lxmf_hash[:16]}", RNS.LOG_DEBUG)
        return True

    def see_peer(self, board_id, destination_hash, identity=None):
        """Backward-compat (v3.6.1). Uses board dest hash as placeholder."""
        h = destination_hash.hex() if isinstance(destination_hash, bytes) else destination_hash
        with self._lock:
            if h in self._peers:
                self._peers[h].touch()
                self._peers[h].boards.add(board_id)
                return
        self.register_from_announce(board_id, h, identity, announce_hash=h)

    def get_peers(self, board_id):
        now = time.time()
        with self._lock:
            hashes = list(self._board_index.get(board_id, set()))
            active = []
            for h in hashes:
                p = self._peers.get(h)
                if p and not p.is_expired(now):
                    active.append(p)
            return sorted(active, key=lambda p: p.last_seen, reverse=True)

    def get_lxmf_peers(self, board_id):
        """Peers reachable via LXMF (have identity, not unreachable, not in backoff)."""
        now = time.time()
        return [
            p for p in self.get_peers(board_id)
            if not self._is_self(p.lxmf_hash)
            and p.identity is not None
            and p.path_state != PathState.UNREACHABLE
            and now >= p.next_retry_at
        ]

    def _peer_quality_score(self, peer: PeerInfo) -> tuple:
        path_rank = {
            PathState.KNOWN: 4,
            PathState.STALE: 3,
            PathState.REQUESTED: 2,
            PathState.UNKNOWN: 1,
            PathState.UNREACHABLE: 0,
        }.get(peer.path_state, 0)
        success_ratio = peer.delivery_successes - peer.delivery_failures
        return (
            path_rank,
            1 if peer.verified else 0,
            success_ratio,
            peer.last_success_at,
            peer.last_seen,
        )

    def get_fetch_peers(self, board_id, count=PAYLOAD_FETCH_PEERS):
        """Return best candidate peers for payload fetches."""
        peers = self.get_lxmf_peers(board_id)

        if not peers:
            with self._lock:
                candidates = [
                    p for p in self._peers.values()
                    if board_id in p.boards
                    and not self._is_self(p.lxmf_hash)
                    and p.identity is not None
                    and p.path_state != PathState.UNREACHABLE
                ]
            peers = candidates

        peers = sorted(peers, key=self._peer_quality_score, reverse=True)
        RNS.log(
            f"get_fetch_peers({board_id[:8]}): returning {len(peers)} peer(s) "
            f"(first {min(count, len(peers))}) — states: {[p.path_state.value for p in peers[:count]]}",
            RNS.LOG_DEBUG,
        )
        return peers[:count]

    def get_direct_have_peers(self, board_id, exclude_hash=None, count=5):
        peers = self.get_lxmf_peers(board_id)
        if exclude_hash:
            peers = [p for p in peers if p.lxmf_hash != exclude_hash]
        scored = [(p, self._peer_quality_score(p)) for p in peers]
        scored.sort(
            key=lambda item: (
                item[0].last_direct_have_at,
                -item[1][0],
                -item[1][2],
                -item[0].last_seen,
            ),
        )
        return [p for p, _ in scored[:count]]

    def mark_direct_have_sent(self, lxmf_hash):
        with self._lock:
            p = self._peers.get(lxmf_hash)
            if p:
                p.last_direct_have_at = time.time()

    def get_replication_targets(self, board_id, exclude_hash=None, count=REPLICATION_FANOUT):
        peers = self.get_lxmf_peers(board_id)
        if exclude_hash:
            peers = [p for p in peers if p.lxmf_hash != exclude_hash]
        peers = sorted(peers, key=self._peer_quality_score, reverse=True)
        return peers[:count]

    def get_peer(self, lxmf_hash):
        with self._lock:
            return self._peers.get(lxmf_hash)

    def peer_count(self, board_id):
        return len(self.get_peers(board_id))

    def unique_peer_count(self, board_ids=None):
        """Return unique non-expired peers across one or more boards."""
        now = time.time()
        with self._lock:
            if board_ids is None:
                hashes = set(self._peers.keys())
            else:
                hashes = set()
                for board_id in board_ids:
                    hashes.update(self._board_index.get(board_id, set()))
            return len([
                peer_hash for peer_hash in hashes
                if (
                    peer_hash in self._peers
                    and not self._is_self(peer_hash)
                    and (now - self._peers[peer_hash].last_seen < 600)
                )
            ])

    def all_board_ids(self):
        with self._lock:
            return list(self._board_index.keys())

    def get_path_summary(self) -> dict[str, int]:
        """Return counts of peers in each PathState (§7.2)."""
        summary = {s.value: 0 for s in PathState}
        now = time.time()
        with self._lock:
            for p in self._peers.values():
                if not p.is_expired(now):
                    summary[p.path_state.value] += 1
        return summary

    def record_delivery_failure(self, lxmf_hash):
        with self._lock:
            p = self._peers.get(lxmf_hash)
            if not p:
                return
            p.path_state = PathState.STALE
            p.retry_count += 1
            p.delivery_failures += 1
            p.last_failure_at = time.time()
            if p.retry_count >= MAX_PATH_RETRIES:
                p.path_state = PathState.UNREACHABLE
                p.next_retry_at = time.time() + PEER_UNREACHABLE_TTL
            else:
                p.next_retry_at = time.time() + p.next_retry_delay()

    def record_delivery_success(self, lxmf_hash):
        with self._lock:
            p = self._peers.get(lxmf_hash)
            if not p:
                return
            p.path_state = PathState.KNOWN
            p.retry_count = 0
            p.verified = True
            p.delivery_successes += 1
            p.last_success_at = time.time()
            p.touch()

    def mark_path_known(self, lxmf_hash):
        with self._lock:
            p = self._peers.get(lxmf_hash)
            if not p:
                return
            p.path_state = PathState.KNOWN
            p.retry_count = 0
            p.touch()

    def sweep_expired(self, now=None):
        if now is None:
            now = time.time()
        with self._lock:
            expired = [h for h, p in self._peers.items() if p.is_expired(now)]
            for h in expired:
                peer = self._peers.pop(h, None)
                if not peer:
                    continue
                for bid in list(peer.boards):
                    members = self._board_index.get(bid)
                    if members is None:
                        continue
                    members.discard(h)
                    if not members:
                        del self._board_index[bid]

    def _enforce_limit(self):
        if len(self._peers) < MAX_PEER_TABLE_SIZE:
            return
        candidates = sorted(self._peers.values(), key=lambda p: (p.verified, p.last_seen))
        victim = candidates[0]
        del self._peers[victim.lxmf_hash]
        for bid in victim.boards:
            self._board_index.get(bid, set()).discard(victim.lxmf_hash)
