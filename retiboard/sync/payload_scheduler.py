"""
Global local-only payload fetch fairness scheduler.

Spec references:
    §7.2  — preserve fairness so a very large attachment transfer cannot
             monopolize all fetch capacity indefinitely.
    §22   — attachment fetch policy remains local-only.
    §15   — applies to payload-plane fetch orchestration only.

The scheduler is intentionally structural-only. It knows blob kind,
expected size, whether the fetch was manually requested by the user,
and low-bandwidth transport state. It never inspects payload content.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field



_SMALL_ATTACHMENT_BYTES = 256 * 1024
_LARGE_ATTACHMENT_BYTES = 8 * 1024 * 1024
_BULK_AGING_SECONDS = 20.0


@dataclass(frozen=True)
class FetchPolicyDecision:
    allowed_auto: bool
    allowed_manual: bool
    priority_class: str
    max_session_parallelism: int
    ui_reason: str = ""


@dataclass
class _SessionState:
    session_id: str
    blob_hash: str
    blob_kind: str
    expected_size: int
    manual_override: bool
    priority_class: str
    max_parallelism: int
    registered_at: float = field(default_factory=time.time)
    active_request_ids: set[str] = field(default_factory=set)


def _is_low_bandwidth() -> bool:
    try:
        from retiboard.transport import is_low_bandwidth
        return bool(is_low_bandwidth())
    except Exception:
        return False


class PayloadFetchScheduler:
    """Node-local fairness scheduler for payload fetch requests."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, _SessionState] = {}
        self._requests: dict[str, str] = {}

    def _limits(self) -> tuple[int, int, int]:
        if _is_low_bandwidth():
            return (2, 1, 1)
        # v3.6.4: Increased limits to better utilize modern hardware/networks
        # and prevent accidental starvation of metadata/text fetches.
        return (12, 4, 2)

    def classify(
        self,
        *,
        blob_kind: str,
        expected_size: int = 0,
        manual_override: bool = False,
    ) -> FetchPolicyDecision:
        blob_kind = str(blob_kind or 'text')
        size = max(0, int(expected_size or 0))
        if blob_kind != 'attachments':
            return FetchPolicyDecision(True, True, 'interactive', 4)
        if manual_override:
            # Explicit user choice should bypass auto-load gating and jump
            # ahead of background bulk work, but still remain bounded.
            max_parallelism = 1 if _is_low_bandwidth() else 4
            return FetchPolicyDecision(False, True, 'manual', max_parallelism)
        if size <= 0 or size <= _SMALL_ATTACHMENT_BYTES:
            return FetchPolicyDecision(True, True, 'interactive', 4)
        if size >= _LARGE_ATTACHMENT_BYTES:
            return FetchPolicyDecision(False, True, 'bulk', 2)
        return FetchPolicyDecision(False, True, 'normal', 1 if _is_low_bandwidth() else 3)

    def register_session(
        self,
        *,
        session_id: str,
        blob_hash: str,
        blob_kind: str,
        expected_size: int = 0,
        manual_override: bool = False,
    ) -> FetchPolicyDecision:
        decision = self.classify(
            blob_kind=blob_kind,
            expected_size=expected_size,
            manual_override=manual_override,
        )
        with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                state = _SessionState(
                    session_id=session_id,
                    blob_hash=blob_hash,
                    blob_kind=str(blob_kind or 'text'),
                    expected_size=max(0, int(expected_size or 0)),
                    manual_override=bool(manual_override),
                    priority_class=decision.priority_class,
                    max_parallelism=max(1, int(decision.max_session_parallelism)),
                )
                self._sessions[session_id] = state
            else:
                state.blob_hash = blob_hash
                state.blob_kind = str(blob_kind or state.blob_kind or 'text')
                state.expected_size = max(0, int(expected_size or state.expected_size))
                state.manual_override = bool(manual_override)
                state.priority_class = decision.priority_class
                state.max_parallelism = max(1, int(decision.max_session_parallelism))
        return decision

    def unregister_session(self, session_id: str) -> None:
        with self._lock:
            state = self._sessions.pop(session_id, None)
            if state is None:
                return
            for request_id in list(state.active_request_ids):
                self._requests.pop(request_id, None)
            state.active_request_ids.clear()

    def release_request(self, request_id: str) -> None:
        if not request_id:
            return
        with self._lock:
            session_id = self._requests.pop(request_id, None)
            if session_id is None:
                return
            state = self._sessions.get(session_id)
            if state is not None:
                state.active_request_ids.discard(request_id)

    def release_session_requests(self, session_id: str) -> None:
        with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                return
            for request_id in list(state.active_request_ids):
                self._requests.pop(request_id, None)
            state.active_request_ids.clear()

    def try_acquire_request(self, session_id: str, request_id: str) -> bool:
        if not session_id or not request_id:
            return False
        with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                return False
            if request_id in state.active_request_ids:
                return True

            global_cap, interactive_cap, bulk_cap = self._limits()
            total_active = sum(len(s.active_request_ids) for s in self._sessions.values())
            if total_active >= global_cap:
                return False
            if len(state.active_request_ids) >= max(1, state.max_parallelism):
                return False

            interactive_active = sum(
                len(s.active_request_ids)
                for s in self._sessions.values()
                if s.priority_class in {'interactive', 'manual'}
            )
            bulk_active = sum(
                len(s.active_request_ids)
                for s in self._sessions.values()
                if s.priority_class == 'bulk'
            )

            if state.priority_class in {'interactive', 'manual'}:
                # Always allow prompt user-visible work if any global slot is free.
                pass
            elif state.priority_class == 'normal':
                # Preserve one slot for interactive/manual work when possible.
                if total_active >= max(1, global_cap - 1) and interactive_active < interactive_cap:
                    return False
            else:  # bulk
                age = time.time() - state.registered_at
                if bulk_active >= bulk_cap:
                    return False
                if total_active >= max(1, global_cap - 1):
                    if interactive_active < interactive_cap:
                        return False
                    if age < _BULK_AGING_SECONDS:
                        return False

            state.active_request_ids.add(request_id)
            self._requests[request_id] = session_id
            return True


_scheduler = PayloadFetchScheduler()


def get_payload_scheduler() -> PayloadFetchScheduler:
    return _scheduler
