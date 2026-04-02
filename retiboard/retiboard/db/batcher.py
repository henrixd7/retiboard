from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from retiboard.chunks.models import ChunkPeerPenaltyRecord, ChunkRequestStateRecord
from retiboard.db.pool import get_board_connection


@dataclass
class ChunkStateBatcher:
    board_id: str
    max_pending: int = 20
    max_interval_seconds: float = 2.0
    _chunk_states: list[ChunkRequestStateRecord] = field(default_factory=list)
    _peer_penalties: list[ChunkPeerPenaltyRecord] = field(default_factory=list)
    _last_flush_at: float = field(default_factory=time.time)
    _flush_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def queue_chunk_state(self, record: ChunkRequestStateRecord) -> None:
        self._chunk_states.append(record)

    def queue_peer_penalty(self, record: ChunkPeerPenaltyRecord) -> None:
        self._peer_penalties.append(record)

    def pending_count(self) -> int:
        return len(self._chunk_states) + len(self._peer_penalties)

    def should_flush(self) -> bool:
        if self.pending_count() >= self.max_pending:
            return True
        if self.pending_count() > 0 and (time.time() - self._last_flush_at) >= self.max_interval_seconds:
            return True
        return False

    async def flush(self) -> int:
        async with self._flush_lock:
            if self.pending_count() == 0:
                return 0
            chunk_states = list(self._chunk_states)
            peer_penalties = list(self._peer_penalties)
            self._chunk_states.clear()
            self._peer_penalties.clear()
            try:
                db = await get_board_connection(self.board_id)
                if chunk_states:
                    await db.executemany(
                        """
                        INSERT OR REPLACE INTO chunk_request_states (
                            session_id, chunk_index, state, assigned_peer_lxmf_hash,
                            request_id, attempt_count, deadline_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                s.session_id,
                                s.chunk_index,
                                s.state,
                                s.assigned_peer_lxmf_hash,
                                s.request_id,
                                int(s.attempt_count),
                                int(s.deadline_at),
                                int(s.updated_at),
                            )
                            for s in chunk_states
                        ],
                    )
                if peer_penalties:
                    await db.executemany(
                        """
                        INSERT OR REPLACE INTO chunk_peer_penalties (
                            board_id, peer_lxmf_hash, timeout_count, invalid_chunk_count,
                            success_count, cooldown_until, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                p.board_id,
                                p.peer_lxmf_hash,
                                int(p.timeout_count),
                                int(p.invalid_chunk_count),
                                int(p.success_count),
                                int(p.cooldown_until),
                                int(p.updated_at),
                            )
                            for p in peer_penalties
                        ],
                    )
                await db.commit()
                self._last_flush_at = time.time()
                return len(chunk_states) + len(peer_penalties)
            except Exception:
                self._chunk_states = chunk_states + self._chunk_states
                self._peer_penalties = peer_penalties + self._peer_penalties
                raise
