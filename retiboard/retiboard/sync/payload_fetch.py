"""
Tier 3 — On-demand payload fetch via RNS Resource transfer.

Spec: §7.1 Tier 3, §6.2, §12, §15

Phase 1 chunk extension:
    - Manifest / chunk control messages go through send_lxmf(), inheriting the
      existing queue/path semantics.
    - Chunk bytes move over the existing dedicated `retiboard.payload`
      destination as raw RNS Resource data.
    - Canonical payload identity remains SHA-256(encrypted_blob). Chunking is
      a transport optimization only.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
import queue
import uuid
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

import RNS

from retiboard.chunks.chunker import build_chunk_manifest
from retiboard.chunks.models import (
    ChunkFetchSession,
    ChunkManifest,
    ChunkManifestEntry,
    ChunkPeerPenaltyRecord,
    ChunkRequestStateRecord,
)
from retiboard.chunks.swarm import RequestPlan, SwarmFetcher, ChunkFetchState, PriorityMode
from retiboard.chunks.reassembly import ReassemblyBuffer
from retiboard.db.batcher import ChunkStateBatcher
from retiboard.chunks.validator import ChunkValidationError, ChunkValidator
from retiboard.db.database import (
    delete_chunk_manifests_for_blobs,
    get_blob_reference,
    load_chunk_fetch_session,
    load_chunk_manifest,
    load_chunk_request_states,
    load_latest_chunk_fetch_session_for_blob,
    load_peer_chunk_availability,
    load_chunk_peer_penalties,
    save_chunk_fetch_session,
    save_chunk_manifest,
    save_chunk_request_state,
    upsert_peer_chunk_availability,
)
from retiboard.db.pool import get_board_connection
from retiboard.storage.payloads import (
    chunk_assembly_path,
    delete_chunk_cache,
    payload_exists,
    payload_path,
    read_payload,
    write_payload,
)
from retiboard.moderation.policy import should_serve_blob
from retiboard.sync import (
    MSG_TYPE_CHUNK_MANIFEST_REQ,
    MSG_TYPE_CHUNK_MANIFEST_RES,
    MSG_TYPE_CHUNK_MANIFEST_UNAV,
    MSG_TYPE_CHUNK_OFFER,
    MSG_TYPE_CHUNK_REQ,
    MSG_TYPE_CHUNK_CANCEL,
    MSG_TYPE_PAYLOAD_REQ,
)
from retiboard.sync.payload_scheduler import get_payload_scheduler
from retiboard.sync.message_queue import SendResult
from retiboard.sync.peers import PathState
from retiboard.transport import is_low_bandwidth, get_max_payload_size

if TYPE_CHECKING:
    pass


# =========================================================================
# Pending whole-payload fetch registry (legacy + fallback)
# =========================================================================

_pending_fetches: dict[str, tuple[asyncio.Event, asyncio.AbstractEventLoop, str]] = {}
_pending_lock = threading.Lock()
_CHUNK_FETCH_RESULT_PAUSED = object()


def register_pending_fetch(content_hash, board_id, loop):
    with _pending_lock:
        if content_hash in _pending_fetches:
            return _pending_fetches[content_hash][0]
        evt = asyncio.Event()
        _pending_fetches[content_hash] = (evt, loop, board_id)
        return evt


def signal_fetch_complete(content_hash):
    with _pending_lock:
        entry = _pending_fetches.pop(content_hash, None)
    if entry:
        evt, loop, _ = entry
        try:
            loop.call_soon_threadsafe(evt.set)
        except RuntimeError:
            evt.set()


def cancel_pending_fetch(content_hash):
    with _pending_lock:
        _pending_fetches.pop(content_hash, None)


def is_fetch_pending(content_hash):
    with _pending_lock:
        return content_hash in _pending_fetches


# =========================================================================
# Phase 1 chunk fetch session registry
# =========================================================================

_CHUNK_FETCH_THRESHOLD = 256 * 1024
_CHUNK_SIZE_DEFAULT = 256 * 1024
_CHUNK_MANIFEST_TIMEOUT = 45.0
_CHUNK_REQUEST_TIMEOUT = 90.0
_CHUNK_REQUEST_TIMEOUT_MAX = 600.0
_CHUNK_TIMEOUT_BYTES_PER_SEC = 131072.0
_CHUNK_OFFER_UPDATE_DEBOUNCE = 0.75


@dataclass
class PendingChunkSession:
    session_id: str
    board_id: str
    blob_hash: str
    blob_kind: str
    assigned_peer_lxmf_hash: str
    event_loop: asyncio.AbstractEventLoop
    manifest_event: asyncio.Event = field(default_factory=asyncio.Event)
    chunk_event: asyncio.Event = field(default_factory=asyncio.Event)
    completed_event: asyncio.Event = field(default_factory=asyncio.Event)
    failed_event: asyncio.Event = field(default_factory=asyncio.Event)
    unavailable_reason: str = ""
    manifest_unavailable_by_peer: dict[str, str] = field(default_factory=dict)
    manifest: Optional[ChunkManifest] = None
    entries: list[ChunkManifestEntry] = field(default_factory=list)
    entries_by_index: dict[int, ChunkManifestEntry] = field(default_factory=dict)
    reassembly: Optional[ReassemblyBuffer] = None
    validator: ChunkValidator = field(default_factory=ChunkValidator)
    stored_chunks: set[int] = field(default_factory=set)
    candidate_peer_lxmf_hashes: list[str] = field(default_factory=list)
    swarm: Optional[SwarmFetcher] = None
    last_error: str = ""
    finalized: bool = False
    cancelled_request_ids: set[str] = field(default_factory=set)
    pending_cancels: list[tuple[str, int, str]] = field(default_factory=list)
    resumed_from_persisted: bool = False
    paused: bool = False
    cancel_requested: bool = False
    last_progress_update_at: float = 0.0
    batcher: Optional[ChunkStateBatcher] = None
    manual_override: bool = False
    priority_class: str = "normal"
    interested_peers: set[str] = field(default_factory=set)
    sync_engine: object | None = None
    last_offer_update_at: float = 0.0
    offer_update_future: object | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    is_running: bool = False


_pending_chunk_sessions: dict[str, PendingChunkSession] = {}
_pending_chunk_by_blob: dict[str, str] = {}
_pending_chunk_lock = threading.Lock()

_payload_destination = None
_payload_announce_lock = threading.Lock()
_last_payload_announce_at = 0.0


@dataclass
class _PayloadSendJob:
    """Unit of work for the per-destination payload sender thread."""
    requester_identity: "RNS.Identity"
    payload_data: bytes
    metadata: dict


_payload_sender_lock = threading.Lock()
_payload_sender_queues: dict[str, "queue.Queue[_PayloadSendJob]"] = {}
_payload_sender_workers: dict[str, threading.Thread] = {}
_cancelled_outbound_chunk_requests: set[str] = set()

# RNS Resource tracking for mid-flight cancellation
_active_inbound_chunk_resources: dict[str, "RNS.Resource"] = {}
_active_outbound_chunk_resources: dict[str, "RNS.Resource"] = {}

# v3.6.3: Track legacy whole-blob fetches for UI visibility.
_active_legacy_fetches: set[str] = set()
_legacy_fetch_lock = threading.Lock()

_PAYLOAD_IDLE_TIMEOUT_INITIAL = 1.0
_PAYLOAD_IDLE_TIMEOUT_ONGOING = 4.0
_PAYLOAD_IDLE_TIMEOUT_MAX = 8.0

_FETCH_TIMEOUT_MIN = 120.0
_FETCH_TIMEOUT_DEFAULT = 300.0
_FETCH_TIMEOUT_MAX = 3600.0
_FETCH_TIMEOUT_POLL_INTERVAL = 15.0
_FETCH_TIMEOUT_BYTES_PER_SEC = 131072.0  # 128 KiB/s conservative baseline


def _next_chunk_timeout(expected_size: int, is_low_bandwidth: bool = False) -> float:
    # v3.6.3: Adaptive timeout floor. 90s for LoRa, 15s for high speed.
    floor = 90.0 if is_low_bandwidth else 15.0
    estimated = expected_size / _CHUNK_TIMEOUT_BYTES_PER_SEC
    return min(_CHUNK_REQUEST_TIMEOUT_MAX, max(floor, estimated))


def register_pending_chunk_session(
    board_id: str,
    blob_hash: str,
    blob_kind: str,
    assigned_peer_lxmf_hash: str,
    loop: asyncio.AbstractEventLoop,
    *,
    session_id: str | None = None,
) -> PendingChunkSession:
    with _pending_chunk_lock:
        existing_id = _pending_chunk_by_blob.get(blob_hash)
        if existing_id and existing_id in _pending_chunk_sessions:
            return _pending_chunk_sessions[existing_id]
        session = PendingChunkSession(
            session_id=session_id or uuid.uuid4().hex,
            board_id=board_id,
            blob_hash=blob_hash,
            blob_kind=blob_kind,
            assigned_peer_lxmf_hash=assigned_peer_lxmf_hash,
            event_loop=loop,
        )
        _pending_chunk_sessions[session.session_id] = session
        _pending_chunk_by_blob[blob_hash] = session.session_id
        return session


def get_pending_chunk_session(session_id: str) -> Optional[PendingChunkSession]:
    with _pending_chunk_lock:
        return _pending_chunk_sessions.get(session_id)


def get_pending_chunk_session_by_blob(blob_hash: str) -> Optional[PendingChunkSession]:
    with _pending_chunk_lock:
        session_id = _pending_chunk_by_blob.get(blob_hash)
        if not session_id:
            return None
        return _pending_chunk_sessions.get(session_id)


def signal_path_discovered(peer_lxmf_hash: str) -> None:
    """Signal all active chunk sessions that a path to a peer is now known."""
    with _pending_chunk_lock:
        for session in _pending_chunk_sessions.values():
            # If the session has a swarm and is tracking this peer, wake it up
            # so it can transition from QUEUED to SENT immediately.
            if session.swarm and peer_lxmf_hash in session.swarm.peers:
                try:
                    session.swarm.wake_up()
                    session.event_loop.call_soon_threadsafe(session.chunk_event.set)
                except Exception:
                    pass


def get_active_chunk_sessions() -> list[dict]:
    """Return a list of summary dicts for all active chunked fetch sessions."""
    active = []

    # v3.6.3: Include legacy whole-blob fetches.
    with _legacy_fetch_lock:
        for blob_hash in _active_legacy_fetches:
            active.append({
                "blob_hash": blob_hash,
                "board_id": "legacy",
                "blob_kind": "unknown",
                "state": "active",
                "stored_chunks": 0,
                "chunk_count": 1,
                "percent_complete": 0,
                "is_legacy": True
            })

    with _pending_chunk_lock:
        for session in _pending_chunk_sessions.values():
            with session.lock:
                state = "active"
                if getattr(session, "paused", False):
                    state = "paused"
                
                stored = len(session.stored_chunks)
                total = session.manifest.chunk_count if session.manifest else 0
                
                active.append({
                    "blob_hash": session.blob_hash,
                    "board_id": session.board_id,
                    "blob_kind": session.blob_kind,
                    "state": state,
                    "stored_chunks": stored,
                    "chunk_count": total,
                    "percent_complete": round((stored / total * 100), 1) if total > 0 else 0,
                })
        return active


async def finish_pending_chunk_session(blob_hash: str) -> None:
    with _pending_chunk_lock:
        session_id = _pending_chunk_by_blob.pop(blob_hash, None)
        session = _pending_chunk_sessions.pop(session_id, None) if session_id else None
    if session is not None:
        scheduler = get_payload_scheduler()
        scheduler.release_session_requests(session.session_id)
        scheduler.unregister_session(session.session_id)
        _cancel_pending_offer_update(session)
    if session and session.batcher is not None:
        try:
            await session.batcher.flush()
        except Exception as exc:
            RNS.log(f"Failed flushing batcher for {blob_hash[:16]} on finish: {exc}", RNS.LOG_WARNING)
    if session:
        try:
            session.event_loop.call_soon_threadsafe(session.completed_event.set)
        except RuntimeError:
            session.completed_event.set()

def fail_pending_chunk_session(blob_hash: str, reason: str) -> None:
    session = get_pending_chunk_session_by_blob(blob_hash)
    if session is None:
        return
    session.last_error = reason
    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None
    if running_loop is session.event_loop:
        session.failed_event.set()
        return
    try:
        session.event_loop.call_soon_threadsafe(session.failed_event.set)
    except RuntimeError:
        session.failed_event.set()


async def cancel_pending_chunk_session(blob_hash: str) -> None:
    with _pending_chunk_lock:
        session_id = _pending_chunk_by_blob.pop(blob_hash, None)
        session = _pending_chunk_sessions.pop(session_id, None) if session_id else None
    if session is not None:
        scheduler = get_payload_scheduler()
        scheduler.release_session_requests(session.session_id)
        scheduler.unregister_session(session.session_id)
        _cancel_pending_offer_update(session)
    if session and session.batcher is not None:
        try:
            await session.batcher.flush()
        except Exception as exc:
            RNS.log(f"Failed flushing batcher for {blob_hash[:16]} on cancel: {exc}", RNS.LOG_WARNING)

async def pause_chunk_fetch(board_id: str, blob_hash: str, *, sync_engine=None) -> bool:
    session = get_pending_chunk_session_by_blob(blob_hash)
    if session is not None and session.board_id == board_id:
        with session.lock:
            session.paused = True
        retired_requests = _retire_active_swarm_requests(session)
        for chunk_index, peer_lxmf_hash, request_id, attempt_count in retired_requests:
            _queue_chunk_state(
                session,
                chunk_index=chunk_index,
                state="cancelled",
                assigned_peer_lxmf_hash=peer_lxmf_hash,
                request_id=request_id,
                attempt_count=attempt_count,
                deadline_at=0,
            )
        if sync_engine is not None:
            await _drain_pending_chunk_cancels(session, sync_engine)
        if session.batcher is not None:
            await session.batcher.flush()
        try:
            session.event_loop.call_soon_threadsafe(session.chunk_event.set)
        except RuntimeError:
            session.chunk_event.set()
        db = await get_board_connection(board_id)
        await save_chunk_fetch_session(
            db,
            ChunkFetchSession(
                session_id=session.session_id,
                board_id=session.board_id,
                blob_hash=session.blob_hash,
                blob_kind=session.blob_kind,
                state="paused",
                request_peer_lxmf_hash=session.assigned_peer_lxmf_hash,
            ),
        )
        return True

    db = await get_board_connection(board_id)
    persisted = await load_latest_chunk_fetch_session_for_blob(db, board_id=board_id, blob_hash=blob_hash)
    if persisted is None:
        return False
    persisted.state = "paused"
    persisted.updated_at = int(time.time())
    await save_chunk_fetch_session(db, persisted)
    return True

async def resume_chunk_fetch(board_id: str, blob_hash: str) -> bool:
    session = get_pending_chunk_session_by_blob(blob_hash)
    if session is not None and session.board_id == board_id:
        with session.lock:
            session.paused = False
            session.cancel_requested = False
        try:
            session.event_loop.call_soon_threadsafe(session.chunk_event.set)
        except RuntimeError:
            session.chunk_event.set()
        db = await get_board_connection(board_id)
        await save_chunk_fetch_session(
            db,
            ChunkFetchSession(
                session_id=session.session_id,
                board_id=session.board_id,
                blob_hash=session.blob_hash,
                blob_kind=session.blob_kind,
                state="started",
                request_peer_lxmf_hash=session.assigned_peer_lxmf_hash,
            ),
        )
        return True

    db = await get_board_connection(board_id)
    persisted = await load_latest_chunk_fetch_session_for_blob(db, board_id=board_id, blob_hash=blob_hash)
    if persisted is None:
        return False
    persisted.state = "started"
    persisted.updated_at = int(time.time())
    await save_chunk_fetch_session(db, persisted)
    return True

async def cancel_chunk_fetch(board_id: str, blob_hash: str, *, sync_engine=None) -> bool:
    session = get_pending_chunk_session_by_blob(blob_hash)
    existed = False
    if session is not None and session.board_id == board_id:
        with session.lock:
            session.cancel_requested = True
            existed = True
        retired_requests = _retire_active_swarm_requests(session)
        for chunk_index, peer_lxmf_hash, request_id, attempt_count in retired_requests:
            _queue_chunk_state(
                session,
                chunk_index=chunk_index,
                state="cancelled",
                assigned_peer_lxmf_hash=peer_lxmf_hash,
                request_id=request_id,
                attempt_count=attempt_count,
                deadline_at=0,
            )
        if sync_engine is not None:
            await _drain_pending_chunk_cancels(session, sync_engine)
        if session.batcher is not None:
            await session.batcher.flush()
        try:
            session.event_loop.call_soon_threadsafe(session.failed_event.set)
            session.event_loop.call_soon_threadsafe(session.chunk_event.set)
        except RuntimeError:
            session.failed_event.set()
            session.chunk_event.set()

    db = await get_board_connection(board_id)
    persisted = await load_latest_chunk_fetch_session_for_blob(db, board_id=board_id, blob_hash=blob_hash)
    existed = existed or persisted is not None
    if existed:
        await delete_chunk_manifests_for_blobs(db, [blob_hash])
    if existed:
        delete_chunk_cache(board_id, blob_hash)
        await cancel_pending_chunk_session(blob_hash)
    return existed

def _queue_chunk_state(
    session: PendingChunkSession,
    *,
    chunk_index: int,
    state: str,
    assigned_peer_lxmf_hash: str = "",
    request_id: str = "",
    attempt_count: int = 0,
    deadline_at: int = 0,
) -> None:
    if session.batcher is None:
        return
    session.batcher.queue_chunk_state(
        ChunkRequestStateRecord(
            session_id=session.session_id,
            chunk_index=int(chunk_index),
            state=state,
            assigned_peer_lxmf_hash=assigned_peer_lxmf_hash,
            request_id=request_id,
            attempt_count=int(attempt_count),
            deadline_at=int(deadline_at),
            updated_at=int(time.time()),
        )
    )


def _queue_swarm_peer_state(session: PendingChunkSession, peer_lxmf_hash: str) -> None:
    if session.batcher is None or session.swarm is None or not peer_lxmf_hash:
        return
    peer = session.swarm.get_peer_state(peer_lxmf_hash)
    if peer is None:
        return
    session.batcher.queue_peer_penalty(
        ChunkPeerPenaltyRecord(
            board_id=session.board_id,
            peer_lxmf_hash=peer_lxmf_hash,
            timeout_count=int(peer.timeout_count),
            invalid_chunk_count=int(peer.invalid_chunk_count),
            success_count=int(peer.success_count),
            cooldown_until=int(peer.cooldown_until),
            updated_at=int(time.time()),
        )
    )


def _cancel_pending_offer_update(session: PendingChunkSession) -> None:
    with session.lock:
        future = session.offer_update_future
        session.offer_update_future = None
    if future is None:
        return
    try:
        future.cancel()
    except Exception:
        pass


def _track_interested_peer(blob_hash: str, peer_lxmf_hash: str) -> None:
    if not peer_lxmf_hash:
        return
    session = get_pending_chunk_session_by_blob(blob_hash)
    if session is None:
        return
    with session.lock:
        session.interested_peers.add(peer_lxmf_hash)


def _read_live_verified_chunk(
    board_id: str,
    blob_hash: str,
    chunk_index: int,
) -> tuple[ChunkManifest, bytes] | None:
    session = get_pending_chunk_session_by_blob(blob_hash)
    if session is None:
        return None

    with session.lock:
        if (
            session.board_id != board_id
            or session.paused
            or session.cancel_requested
            or session.manifest is None
            or session.reassembly is None
            or chunk_index not in session.stored_chunks
        ):
            return None
        manifest = session.manifest
        entry = session.entries_by_index.get(chunk_index)
        reassembly = session.reassembly

    if entry is None:
        return None
    data = reassembly.read_chunk(entry.offset, entry.size)
    if data is None:
        return None
    computed = hashlib.sha256(data).hexdigest()
    if computed != entry.chunk_hash:
        RNS.log(
            f"Refused live partial chunk {chunk_index} for {blob_hash[:16]}: "
            "assembly bytes failed manifest hash verification",
            RNS.LOG_WARNING,
        )
        return None
    return manifest, data


async def _send_incremental_chunk_offer(session: PendingChunkSession, *, force: bool = False) -> None:
    while True:
        with session.lock:
            sync_engine = session.sync_engine
            manifest = session.manifest
            peers = sorted(session.interested_peers)
            paused = session.paused
            cancelled = session.cancel_requested
            last_offer_update_at = session.last_offer_update_at
        if sync_engine is None or manifest is None or paused or cancelled or not peers:
            with session.lock:
                session.offer_update_future = None
            return

        now = time.monotonic()
        delay = 0.0 if force else max(0.0, _CHUNK_OFFER_UPDATE_DEBOUNCE - (now - last_offer_update_at))
        if delay > 0:
            await asyncio.sleep(delay)
            force = True
            continue

        with session.lock:
            sync_engine = session.sync_engine
            manifest = session.manifest
            peers = sorted(session.interested_peers)
            paused = session.paused
            cancelled = session.cancel_requested
            if sync_engine is None or manifest is None or paused or cancelled or not peers:
                session.offer_update_future = None
                return
            ranges = _compress_chunk_indexes(session.stored_chunks)
            complete = (
                manifest.chunk_count > 0
                and len(session.stored_chunks) >= manifest.chunk_count
            )
            session.last_offer_update_at = time.monotonic()
            session.offer_update_future = None

        for peer_lxmf_hash in peers:
            await _send_chunk_offer(
                sync_engine,
                peer_lxmf_hash,
                session.board_id,
                session.blob_hash,
                manifest.chunk_count,
                complete,
                ranges,
            )
        return


def _schedule_incremental_chunk_offer(session: PendingChunkSession, *, force: bool = False) -> None:
    with session.lock:
        sync_engine = session.sync_engine
        manifest = session.manifest
        peers = bool(session.interested_peers)
        future = session.offer_update_future
        loop = session.event_loop
    if sync_engine is None or manifest is None or not peers:
        return
    if future is not None and not getattr(future, "done", lambda: True)() and not force:
        return
    new_future = asyncio.run_coroutine_threadsafe(
        _send_incremental_chunk_offer(session, force=force),
        loop,
    )
    with session.lock:
        session.offer_update_future = new_future


def _retire_active_swarm_requests(session: PendingChunkSession) -> list[tuple[int, str, str, int]]:
    """Locally retire live requests so pause/resume does not wait for timeouts."""
    retired: list[tuple[int, str, str, int]] = []
    resources_to_cancel: list[object] = []
    scheduler = get_payload_scheduler()
    with session.lock:
        if session.swarm is None:
            return retired
        for request in list(session.swarm.requests.values()):
            session.pending_cancels.append((request.peer_lxmf_hash, request.chunk_index, request.request_id))
            session.cancelled_request_ids.add(request.request_id)
            scheduler.release_request(request.request_id)
            with _payload_sender_lock:
                res = _active_inbound_chunk_resources.get(request.request_id)
            if res is not None:
                resources_to_cancel.append(res)
            chunk_state = session.swarm.chunks.get(request.chunk_index)
            attempt_count = chunk_state.attempt_count if chunk_state is not None else 0
            session.swarm.mark_cancelled(request.request_id)
            retired.append((request.chunk_index, request.peer_lxmf_hash, request.request_id, attempt_count))
    for res in resources_to_cancel:
        try:
            res.cancel()
        except Exception:
            pass
    return retired


def _build_live_progress_snapshot(session: PendingChunkSession) -> dict:
    with session.lock:
        snapshot = {
            "board_id": session.board_id,
            "blob_hash": session.blob_hash,
            "session_id": session.session_id,
            "state": "paused" if session.paused else ("manifest_pending" if session.manifest is None else "fetching"),
            "blob_kind": session.blob_kind,
            "resumed_from_persisted": bool(session.resumed_from_persisted),
            "last_error": session.last_error,
        }
        if session.manifest is not None:
            total_chunks = int(session.manifest.chunk_count)
            stored_chunks = len(session.stored_chunks)
            percent = int((stored_chunks * 100) / total_chunks) if total_chunks > 0 else 0
            snapshot.update({
                "chunk_count": total_chunks,
                "stored_chunks": stored_chunks,
                "percent_complete": percent,
            })
        else:
            snapshot.update({
                "chunk_count": 0,
                "stored_chunks": 0,
                "percent_complete": 0,
            })
        if session.finalized or payload_exists(session.board_id, session.blob_hash):
            snapshot["state"] = "complete"
            snapshot["percent_complete"] = 100
        elif session.cancel_requested:
            snapshot["state"] = "cancelled"
        elif session.failed_event.is_set():
            snapshot["state"] = "failed"
        if session.swarm is not None:
            snapshot.update(session.swarm.progress_snapshot())
        else:
            snapshot.setdefault("requested_chunks", 0)
            snapshot.setdefault("active_requests", 0)
            snapshot.setdefault("peer_count", len(session.candidate_peer_lxmf_hashes))
            snapshot.setdefault("available_peers", len(session.candidate_peer_lxmf_hashes))
            snapshot.setdefault("cooled_down_peers", 0)
            snapshot.setdefault("complete", snapshot["state"] == "complete")
    snapshot["updated_at"] = int(time.time())
    return snapshot


async def _load_persisted_progress_snapshot(board_id: str, blob_hash: str) -> dict | None:
    """Build a progress snapshot from persisted chunk session state."""
    db = await get_board_connection(board_id)
    persisted = await load_latest_chunk_fetch_session_for_blob(db, board_id=board_id, blob_hash=blob_hash)
    if persisted is None:
        return None

    states = await load_chunk_request_states(db, persisted.session_id)
    stored_chunks = sum(1 for row in states if row.state == "stored")
    requested_chunks = sum(1 for row in states if row.state == "requested")
    resumed_from_persisted = True
    if persisted.state == "paused":
        requested_chunks = 0
        resumed_from_persisted = False

    loaded = await load_chunk_manifest(db, blob_hash)
    if loaded:
        manifest, _ = loaded
        chunk_count = manifest.chunk_count
        blob_kind = manifest.blob_kind
    else:
        chunk_count = max((row.chunk_index for row in states), default=-1) + 1
        blob_kind = persisted.blob_kind

    percent = int((stored_chunks * 100) / chunk_count) if chunk_count > 0 else 0

    return {
        "board_id": board_id,
        "blob_hash": blob_hash,
        "session_id": persisted.session_id,
        "state": persisted.state,
        "blob_kind": blob_kind,
        "chunk_count": chunk_count,
        "stored_chunks": stored_chunks,
        "requested_chunks": requested_chunks,
        "active_requests": requested_chunks,
        "peer_count": 0,
        "available_peers": 0,
        "cooled_down_peers": 0,
        "percent_complete": percent,
        "complete": payload_exists(board_id, blob_hash),
        "resumed_from_persisted": resumed_from_persisted,
        "last_error": "",
        "updated_at": int(time.time()),
    }


def _should_prefer_persisted_progress(live: dict, persisted: dict | None) -> bool:
    """Prefer persisted progress over placeholder live snapshots during restore."""
    if bool(live.get("complete")):
        return False
    if str(live.get("state") or "") not in {"manifest_pending", "fetching"}:
        return False
    if int(live.get("chunk_count") or 0) > 0:
        return False
    if persisted is None:
        return False
    persisted_chunks = int(persisted.get("chunk_count") or 0)
    persisted_stored = int(persisted.get("stored_chunks") or 0)
    persisted_percent = int(persisted.get("percent_complete") or 0)
    return persisted_chunks > 0 or persisted_stored > 0 or persisted_percent > 0


async def get_chunk_fetch_progress(board_id: str, blob_hash: str) -> dict | None:
    session = get_pending_chunk_session_by_blob(blob_hash)
    if session is not None and session.board_id == board_id:
        live_snapshot = _build_live_progress_snapshot(session)
        live_is_placeholder = (
            not bool(live_snapshot.get("complete"))
            and str(live_snapshot.get("state") or "") in {"manifest_pending", "fetching"}
            and int(live_snapshot.get("chunk_count") or 0) == 0
        )
        if live_is_placeholder:
            persisted_snapshot = await _load_persisted_progress_snapshot(board_id, blob_hash)
            if _should_prefer_persisted_progress(live_snapshot, persisted_snapshot):
                return persisted_snapshot
        return live_snapshot

    if payload_exists(board_id, blob_hash):
        # Try to get actual chunk counts if possible even when complete
        db = await get_board_connection(board_id)
        manifest_loaded = await load_chunk_manifest(db, blob_hash)
        c_count = 0
        if manifest_loaded:
            manifest, _ = manifest_loaded
            c_count = manifest.chunk_count
            
        return {
            "board_id": board_id,
            "blob_hash": blob_hash,
            "session_id": "",
            "state": "complete",
            "chunk_count": c_count,
            "stored_chunks": c_count,
            "requested_chunks": 0,
            "active_requests": 0,
            "peer_count": 0,
            "available_peers": 0,
            "cooled_down_peers": 0,
            "percent_complete": 100,
            "complete": True,
            "resumed_from_persisted": False,
            "last_error": "",
            "updated_at": int(time.time()),
        }

    return await _load_persisted_progress_snapshot(board_id, blob_hash)


def init_payload_destination(identity: RNS.Identity) -> RNS.Destination:
    global _payload_destination

    dest = RNS.Destination(
        identity,
        RNS.Destination.IN,
        RNS.Destination.SINGLE,
        "retiboard",
        "payload",
    )

    dest.set_link_established_callback(_on_payload_link_established)
    dest.announce()

    _payload_destination = dest

    RNS.log(
        f"Payload transfer destination ready: "
        f"{RNS.prettyhexrep(dest.hash)}",
        RNS.LOG_DEBUG,
    )
    return dest


def announce_payload_destination(force: bool = False, min_interval: float = 5.0) -> bool:
    global _last_payload_announce_at

    if not _payload_destination:
        return False

    now = time.time()
    with _payload_announce_lock:
        if not force and (now - _last_payload_announce_at) < min_interval:
            RNS.log(
                f"Skipped payload destination re-announce ({now - _last_payload_announce_at:.1f}s since last)",
                RNS.LOG_DEBUG,
            )
            return False
        _payload_destination.announce()
        _last_payload_announce_at = now

    RNS.log("Re-announced payload transfer destination", RNS.LOG_DEBUG)
    return True


def get_payload_dest_hash() -> str:
    if _payload_destination:
        return _payload_destination.hexhash
    return ""


def _resource_started_callback(resource) -> None:
    try:
        raw_meta = getattr(resource, 'metadata', None)
        if not raw_meta:
            return
        meta = json.loads(raw_meta.decode("utf-8")) if isinstance(raw_meta, bytes) else raw_meta
        request_id = str(meta.get("request_id", ""))
        blob_hash = str(meta.get("blob_hash", ""))
        if request_id:
            with _payload_sender_lock:
                _active_inbound_chunk_resources[request_id] = resource

            session = get_pending_chunk_session_by_blob(blob_hash)
            should_cancel = False
            if session:
                with session.lock:
                    should_cancel = (
                        getattr(session, 'paused', False)
                        or getattr(session, 'cancel_requested', False)
                        or request_id in session.cancelled_request_ids
                    )
            if should_cancel:
                try:
                    resource.cancel()
                except Exception:
                    pass
    except Exception:
        pass


def _on_payload_link_established(link: RNS.Link) -> None:
    link.set_resource_strategy(RNS.Link.ACCEPT_ALL)
    link.set_resource_started_callback(_resource_started_callback)
    link.set_resource_concluded_callback(_resource_concluded_callback)
    RNS.log("Incoming payload transfer link established", RNS.LOG_DEBUG)


def _resource_concluded_callback(resource) -> None:
    try:
        meta = None
        try:
            raw_meta = getattr(resource, 'metadata', None)
            if raw_meta is None:
                return
            if isinstance(raw_meta, bytes):
                meta = json.loads(raw_meta.decode("utf-8"))
            elif isinstance(raw_meta, dict):
                meta = raw_meta
            else:
                return
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
            return

        if not isinstance(meta, dict):
            return

        # Clean up inbound tracking map
        request_id = str(meta.get("request_id", ""))
        if request_id:
            with _payload_sender_lock:
                _active_inbound_chunk_resources.pop(request_id, None)

        if getattr(resource, 'status', None) != RNS.Resource.COMPLETE:
            blob_hash = meta.get("blob_hash") or meta.get("content_hash")
            session = get_pending_chunk_session_by_blob(str(blob_hash)) if blob_hash else None
            if session is not None and request_id:
                with session.lock:
                    benign_cancel = (
                        session.paused
                        or session.cancel_requested
                        or request_id in session.cancelled_request_ids
                    )
                if benign_cancel:
                    return
            if blob_hash:
                fail_pending_chunk_session(blob_hash, f"resource status={getattr(resource, 'status', 'unknown')}")
                signal_fetch_complete(blob_hash)
            return
        else:
            blob_hash = meta.get("blob_hash") or meta.get("content_hash")
            chunk_index = meta.get("chunk_index")
            if chunk_index is not None:
                RNS.log(f"CHUNK_RES success: {str(blob_hash)[:12]} idx {chunk_index}", RNS.LOG_DEBUG)
            else:
                RNS.log(f"PAYLOAD_RES success: {str(blob_hash)[:12]}", RNS.LOG_DEBUG)

        raw = getattr(resource, 'data', b"")
        if hasattr(raw, 'read'):
            data = raw.read()
        elif isinstance(raw, (bytes, bytearray)):
            data = bytes(raw)
        else:
            data = bytes(raw)

        if {"blob_hash", "chunk_index", "request_id"}.issubset(meta.keys()):
            _handle_incoming_chunk_resource(meta, data)
            return

        content_hash = meta.get("content_hash", "")
        board_id = meta.get("board_id", "")
        if not content_hash or not board_id:
            return

        computed = hashlib.sha256(data).hexdigest()
        if computed != content_hash:
            RNS.log(f"Payload Resource hash mismatch: expected {content_hash[:16]}, got {computed[:16]}", RNS.LOG_WARNING)
            signal_fetch_complete(content_hash)
            return

        write_payload(board_id, content_hash, data, verify_hash=False)
        RNS.log(f"Stored payload {content_hash[:16]} ({len(data)} bytes) via RNS Resource", RNS.LOG_DEBUG)
        signal_fetch_complete(content_hash)

    except Exception as e:
        RNS.log(f"Resource concluded callback error: {e}", RNS.LOG_WARNING)


def _handle_incoming_chunk_resource(meta: dict, data: bytes) -> None:
    blob_hash = str(meta.get("blob_hash", ""))
    board_id = str(meta.get("board_id", ""))
    request_id = str(meta.get("request_id", ""))
    chunk_index = int(meta.get("chunk_index", -1))
    peer_lxmf_hash = str(meta.get("peer_lxmf_hash", ""))

    session = get_pending_chunk_session_by_blob(blob_hash)
    if session is None:
        RNS.log(f"Ignoring chunk for inactive blob session {blob_hash[:16]}", RNS.LOG_DEBUG)
        return

    pending_signals: list[asyncio.Event] =[]
    finalize_info: tuple[str, str] | None = None
    schedule_offer_update = False
    force_offer_update = False

    with session.lock:
        if board_id != session.board_id or session.manifest is None or session.reassembly is None or session.swarm is None:
            return

        request = session.swarm.lookup_request(request_id)
        if request is None:
            RNS.log(f"Ignoring stale/untracked chunk request {request_id[:12]} for {blob_hash[:16]}", RNS.LOG_DEBUG)
            return

        if session.paused or session.cancel_requested or request_id in session.cancelled_request_ids:
            get_payload_scheduler().release_request(request_id)
            session.cancelled_request_ids.add(request_id)
            session.swarm.mark_send_failed(
                RequestPlan(
                    request_id=request_id,
                    peer_lxmf_hash=request.peer_lxmf_hash,
                    chunk_index=request.chunk_index,
                    timeout_seconds=0.0,
                )
            )
            try:
                session.event_loop.call_soon_threadsafe(session.chunk_event.set)
            except RuntimeError:
                session.chunk_event.set()
            return

        assigned_peer = request.peer_lxmf_hash
        chunk_state_obj = session.swarm.chunks.get(chunk_index)
        attempt_count = chunk_state_obj.attempt_count if chunk_state_obj is not None else 0

        if request.chunk_index != chunk_index:
            get_payload_scheduler().release_request(request_id)
            session.swarm.mark_invalid(request_id)
            _queue_swarm_peer_state(session, assigned_peer)
            _queue_chunk_state(session, chunk_index=chunk_index, state="missing", assigned_peer_lxmf_hash="", request_id="", attempt_count=attempt_count, deadline_at=0)
            pending_signals.append(session.chunk_event)
        else:
            try:
                checked = session.validator.prevalidate(
                    manifest=session.manifest,
                    entries_by_index=session.entries_by_index,
                    chunk_index=chunk_index,
                    peer_lxmf_hash=peer_lxmf_hash or assigned_peer,
                    assigned_peer_lxmf_hash=assigned_peer,
                    data=data,
                )
                session.reassembly.write_verified_chunk(chunk_index, checked.offset, data)
                session.stored_chunks.add(chunk_index)
                get_payload_scheduler().release_request(request_id)
                cancelled_requests = session.swarm.mark_chunk_stored(request_id)
                chunk_state = session.swarm.chunks.get(chunk_index)
                stored_attempt_count = chunk_state.attempt_count if chunk_state is not None else attempt_count
                _queue_swarm_peer_state(session, assigned_peer)
                _queue_chunk_state(session, chunk_index=chunk_index, state="stored", assigned_peer_lxmf_hash=assigned_peer, request_id=request_id, attempt_count=stored_attempt_count, deadline_at=0)
                for sibling in cancelled_requests:
                    get_payload_scheduler().release_request(sibling.request_id)
                    session.cancelled_request_ids.add(sibling.request_id)
                    session.pending_cancels.append((sibling.peer_lxmf_hash, sibling.chunk_index, sibling.request_id))
                    sibling_state = session.swarm.chunks.get(sibling.chunk_index)
                    sibling_attempt_count = sibling_state.attempt_count if sibling_state is not None else 0
                    _queue_chunk_state(session, chunk_index=sibling.chunk_index, state="cancelled", assigned_peer_lxmf_hash=sibling.peer_lxmf_hash, request_id=sibling.request_id, attempt_count=sibling_attempt_count, deadline_at=0)
                pending_signals.append(session.chunk_event)
                schedule_offer_update = True
                force_offer_update = session.reassembly.is_complete()

                if session.reassembly.is_complete() and not session.finalized:
                    final_path = payload_path(session.board_id, session.blob_hash)
                    session.reassembly.finalize(session.blob_hash, final_path)
                    session.finalized = True
                    finalize_info = (session.board_id, session.blob_hash)
            except ChunkValidationError as e:
                get_payload_scheduler().release_request(request_id)
                session.swarm.mark_invalid(request_id)
                invalid_state = session.swarm.chunks.get(chunk_index)
                invalid_attempt_count = invalid_state.attempt_count if invalid_state is not None else 0
                _queue_swarm_peer_state(session, assigned_peer)
                _queue_chunk_state(session, chunk_index=chunk_index, state="rejected_invalid", assigned_peer_lxmf_hash=assigned_peer, request_id=request_id, attempt_count=invalid_attempt_count, deadline_at=0)
                RNS.log(f"Rejected invalid chunk {chunk_index} for {blob_hash[:16]}: {e}", RNS.LOG_WARNING)
                pending_signals.append(session.chunk_event)
            except Exception as e:
                fail_pending_chunk_session(blob_hash, f"chunk store failed: {e}")
                return

    for evt in pending_signals:
        try:
            session.event_loop.call_soon_threadsafe(evt.set)
        except RuntimeError:
            evt.set()

    if schedule_offer_update:
        _schedule_incremental_chunk_offer(
            session,
            force=force_offer_update,
        )

    if finalize_info is not None:
        _, fin_blob_hash = finalize_info
        RNS.log(f"Stored chunked payload {fin_blob_hash[:16]} via swarm reassembly", RNS.LOG_DEBUG)
        try:
            session.event_loop.call_soon_threadsafe(session.completed_event.set)
        except RuntimeError:
            session.completed_event.set()
        try:
            session.event_loop.call_soon_threadsafe(session.chunk_event.set)
        except RuntimeError:
            session.chunk_event.set()
        asyncio.run_coroutine_threadsafe(finish_pending_chunk_session(fin_blob_hash), session.event_loop)

def _enqueue_payload_resource_send(requester_identity, payload_data, metadata: dict):
    try:
        dest = RNS.Destination(
            requester_identity,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            "retiboard",
            "payload",
        )
        dest_hex = dest.hexhash
    except Exception as e:
        RNS.log(f"Failed to prepare payload destination: {e}", RNS.LOG_WARNING)
        return

    job = _PayloadSendJob(
        requester_identity=requester_identity,
        payload_data=payload_data,
        metadata=metadata,
    )

    with _payload_sender_lock:
        q = _payload_sender_queues.get(dest_hex)
        if q is None:
            q = queue.Queue()
            _payload_sender_queues[dest_hex] = q
        q.put(job)

        worker = _payload_sender_workers.get(dest_hex)
        if worker is None or not worker.is_alive():
            worker = threading.Thread(
                target=_payload_sender_worker,
                args=(dest_hex, requester_identity),
                daemon=True,
            )
            _payload_sender_workers[dest_hex] = worker
            worker.start()


def _payload_sender_worker(dest_hex: str, requester_identity: RNS.Identity) -> None:
    jobs_served = 0
    link = None
    q = None
    max_retries = 3
    retry_count = 0

    try:
        with _payload_sender_lock:
            q = _payload_sender_queues.get(dest_hex)
        if q is None:
            return

        dest = RNS.Destination(
            requester_identity,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            "retiboard",
            "payload",
        )

        while retry_count < max_retries:
            if not RNS.Transport.has_path(dest.hash):
                RNS.log(f"Waiting for path to payload dest {dest_hex[:16]} (retry {retry_count})...", RNS.LOG_DEBUG)
                RNS.Transport.request_path(dest.hash)
                start = time.time()
                while not RNS.Transport.has_path(dest.hash):
                    if time.time() - start > 30:
                        break
                    time.sleep(0.5)

            if RNS.Transport.has_path(dest.hash):
                link = RNS.Link(dest)
                start = time.time()
                while link.status != RNS.Link.ACTIVE:
                    if time.time() - start > 30 or link.status == RNS.Link.CLOSED:
                        break
                    time.sleep(0.2)
                
                if link.status == RNS.Link.ACTIVE:
                    break
            
            retry_count += 1
            if retry_count < max_retries:
                time.sleep(5.0 * (2 ** retry_count)) # Exponential backoff

        if not link or link.status != RNS.Link.ACTIVE:
            RNS.log(f"Failed to establish payload link to {dest_hex[:16]} after {max_retries} retries", RNS.LOG_WARNING)
            return

        while True:
            idle_timeout = _PAYLOAD_IDLE_TIMEOUT_INITIAL if jobs_served == 0 else min(_PAYLOAD_IDLE_TIMEOUT_ONGOING, _PAYLOAD_IDLE_TIMEOUT_MAX)
            try:
                job = q.get(timeout=idle_timeout)
            except queue.Empty:
                break
            # ... (rest of the loop remains same)
            request_id = str(job.metadata.get("request_id", ""))
            if request_id and request_id in _cancelled_outbound_chunk_requests:
                with _payload_sender_lock:
                    _cancelled_outbound_chunk_requests.discard(request_id)
                continue

            event = threading.Event()
            outcome = {"status": None}

            def _callback(resource, done=event, state=outcome, req_id=request_id):
                state["status"] = getattr(resource, 'status', None)
                if req_id:
                    with _payload_sender_lock:
                        _active_outbound_chunk_resources.pop(req_id, None)
                done.set()

            meta = json.dumps(job.metadata, separators=(",", ":")).encode("utf-8")

            try:
                res = RNS.Resource(
                    job.payload_data,
                    link,
                    metadata=meta,
                    callback=_callback,
                    auto_compress=False,
                )
                if request_id:
                    with _payload_sender_lock:
                        _active_outbound_chunk_resources[request_id] = res
            except Exception as e:
                RNS.log(f"Error initiating payload Resource: {e}", RNS.LOG_WARNING)
                continue

            timeout = max(60.0, min(300.0, len(job.payload_data) / 32768.0))
            if not event.wait(timeout=timeout):
                RNS.log("Payload Resource transfer timed out", RNS.LOG_WARNING)
                break

            if outcome["status"] == RNS.Resource.COMPLETE:
                jobs_served += 1
            else:
                break
    except Exception as e:
        RNS.log(f"Error in payload sender worker for {dest_hex[:16]}: {e}", RNS.LOG_WARNING)
    finally:
        try:
            if link and link.status == RNS.Link.ACTIVE:
                link.teardown()
        except Exception:
            pass
        with _payload_sender_lock:
            _payload_sender_workers.pop(dest_hex, None)
            q = _payload_sender_queues.get(dest_hex)
            if q is not None and q.empty():
                _payload_sender_queues.pop(dest_hex, None)


# =========================================================================
# LXMF control-plane handlers
# =========================================================================

async def handle_payload_request_lxmf(content, source_hash, source_identity, sync_engine):
    try:
        req = json.loads(content)
    except json.JSONDecodeError:
        RNS.log("Invalid JSON in PAYLOAD_REQUEST", RNS.LOG_WARNING)
        return

    content_hash = req.get("content_hash", "")
    board_id = req.get("board_id", "")

    RNS.log(
        f"Received PAYLOAD_REQ for {content_hash[:12]} from "
        f"{source_hash.hex()[:16] if source_hash else 'unknown'}",
        RNS.LOG_DEBUG,
    )

    if not content_hash or not board_id:
        return
    if source_identity is None:
        requester_hex = source_hash.hex() if isinstance(source_hash, bytes) else str(source_hash)
        RNS.log(
            f"Dropping PAYLOAD_REQUEST for {content_hash[:16]} from {requester_hex[:16]}: missing source identity",
            RNS.LOG_WARNING,
        )
        return

    db = await get_board_connection(board_id)
    decision = await should_serve_blob(db, content_hash)
    if not decision.allowed:
        return
    
    payload_data = read_payload(board_id, content_hash)
    if payload_data is None:
        return

    _enqueue_payload_resource_send(
        source_identity,
        payload_data,
        {"content_hash": content_hash, "board_id": board_id},
    )


async def handle_chunk_manifest_request_lxmf(content, source_hash, source_identity, sync_engine):
    try:
        req = json.loads(content)
    except json.JSONDecodeError:
        return

    board_id = str(req.get("board_id", ""))
    blob_hash = str(req.get("blob_hash", ""))

    RNS.log(
        f"Received CHUNK_MANIFEST_REQ for {blob_hash[:12]} from "
        f"{source_hash.hex()[:16] if source_hash else 'unknown'}",
        RNS.LOG_DEBUG,
    )
    if not board_id or not blob_hash or sync_engine is None:
        return

    requester_hex = source_hash.hex() if isinstance(source_hash, bytes) else str(source_hash)

    if source_identity is None:
        RNS.log(
            f"Dropping CHUNK_MANIFEST_REQUEST for {blob_hash[:16]} from {requester_hex[:16]}: missing source identity",
            RNS.LOG_WARNING,
        )
        return

    _track_interested_peer(blob_hash, requester_hex)
    
    # v3.6.3: If we have an active session for this blob, trigger an 
    # immediate incremental offer update so the requester knows our 
    # current progress immediately.
    session = get_pending_chunk_session_by_blob(blob_hash)
    if session:
        _schedule_incremental_chunk_offer(session, force=True)

    db = await get_board_connection(board_id)
    decision = await should_serve_blob(db, blob_hash)
    ref = await get_blob_reference(db, blob_hash)
    if not decision.allowed:
        if decision.reason in {"not_found", "withheld_local_policy", "policy_rejected", "abandoned", "pruned"}:
            reason = decision.reason
        else:
            reason = "withheld_local_policy"
        await _send_manifest_unavailable(sync_engine, source_hash, board_id, blob_hash, reason)
        return

    local_manifest, local_entries, local_ranges, local_complete = await _get_local_chunk_offer(board_id, blob_hash)
    if local_manifest is None:
        reason = "not_found" if ref is None else "pruned"
        await _send_manifest_unavailable(sync_engine, source_hash, board_id, blob_hash, reason)
        return

    payload = {
        "board_id": local_manifest.board_id,
        "blob_hash": local_manifest.blob_hash,
        "blob_size": local_manifest.blob_size,
        "chunk_size": local_manifest.chunk_size,
        "chunk_count": local_manifest.chunk_count,
        "merkle_root": local_manifest.merkle_root,
        "blob_kind": local_manifest.blob_kind,
        "entries":[
            {
                "blob_hash": e.blob_hash,
                "chunk_index": e.chunk_index,
                "offset": e.offset,
                "size": e.size,
                "chunk_hash": e.chunk_hash,
            }
            for e in local_entries
        ],
    }
    sync_engine.send_lxmf(requester_hex, json.dumps(payload, separators=(",", ":")).encode("utf-8"), MSG_TYPE_CHUNK_MANIFEST_RES, priority=_control_priority())
    await _send_chunk_offer(sync_engine, requester_hex, board_id, blob_hash, local_manifest.chunk_count, local_complete, local_ranges)

async def _send_manifest_unavailable(sync_engine, source_hash, board_id: str, blob_hash: str, reason: str) -> None:
    requester_hex = source_hash.hex() if isinstance(source_hash, bytes) else str(source_hash)
    payload = {
        "board_id": board_id,
        "blob_hash": blob_hash,
        "reason": reason,
    }
    sync_engine.send_lxmf(
        requester_hex,
        json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        MSG_TYPE_CHUNK_MANIFEST_UNAV,
        priority=_control_priority(),
    )


async def _send_chunk_offer(sync_engine, destination_hash: str, board_id: str, blob_hash: str, chunk_count: int, complete: bool, ranges: list[tuple[int, int]]) -> None:
    payload = {
        "board_id": board_id,
        "blob_hash": blob_hash,
        "chunk_count": int(chunk_count),
        "complete": bool(complete),
        "ranges": [[int(start), int(end)] for start, end in ranges],
    }
    sync_engine.send_lxmf(
        destination_hash,
        json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        MSG_TYPE_CHUNK_OFFER,
        priority=_control_priority(),
    )


def _manifest_entries_match(
    existing_manifest: ChunkManifest,
    existing_entries: list[ChunkManifestEntry],
    incoming_manifest: ChunkManifest,
    incoming_entries: list[ChunkManifestEntry],
) -> bool:
    """Compare canonical manifest structure without touching payload state."""
    if (
        existing_manifest.blob_hash != incoming_manifest.blob_hash
        or existing_manifest.blob_size != incoming_manifest.blob_size
        or existing_manifest.chunk_size != incoming_manifest.chunk_size
        or existing_manifest.chunk_count != incoming_manifest.chunk_count
        or existing_manifest.blob_kind != incoming_manifest.blob_kind
    ):
        return False
    if len(existing_entries) != len(incoming_entries):
        return False
    existing_by_index = {entry.chunk_index: entry for entry in existing_entries}
    for entry in incoming_entries:
        current = existing_by_index.get(entry.chunk_index)
        if current is None:
            return False
        if (
            current.offset != entry.offset
            or current.size != entry.size
            or current.chunk_hash != entry.chunk_hash
        ):
            return False
    return True


async def handle_chunk_manifest_response_lxmf(content, source_hash) -> None:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return
    blob_hash = str(payload.get("blob_hash", ""))
    board_id = str(payload.get("board_id", ""))
    session = get_pending_chunk_session_by_blob(blob_hash)
    if session is None or session.board_id != board_id:
        return

    entries =[
        ChunkManifestEntry(
            blob_hash=str(e["blob_hash"]),
            chunk_index=int(e["chunk_index"]),
            offset=int(e["offset"]),
            size=int(e["size"]),
            chunk_hash=str(e["chunk_hash"]),
        )
        for e in payload.get("entries",[])
    ]
    manifest = ChunkManifest(
        manifest_version=1,
        board_id=board_id,
        post_id="",
        thread_id="",
        blob_kind=str(payload.get("blob_kind", session.blob_kind or "text")),
        blob_hash=blob_hash,
        blob_size=int(payload.get("blob_size", 0)),
        chunk_size=int(payload.get("chunk_size", 0)),
        chunk_count=int(payload.get("chunk_count", 0)),
        merkle_root=payload.get("merkle_root"),
    )

    # v3.6.3: Adversarial hardening — Resource Exhaustion (§15).
    # Reject manifests for blobs exceeding our transport-aware max size.
    max_payload_size = get_max_payload_size()
    if manifest.blob_size > max_payload_size:
        RNS.log(
            f"Rejected manifest for {blob_hash[:16]}: size {manifest.blob_size} "
            f"exceeds local max {max_payload_size}",
            RNS.LOG_WARNING,
        )
        # Fail the session so the caller stops waiting.
        fail_pending_chunk_session(blob_hash, "Oversized blob manifest rejected")
        return

    source_peer = source_hash.hex() if isinstance(source_hash, bytes) else str(source_hash)
    with session.lock:
        existing_manifest = session.manifest
        existing_entries = list(session.entries)
    if existing_manifest is not None:
        if not _manifest_entries_match(existing_manifest, existing_entries, manifest, entries):
            RNS.log(
                f"Ignoring conflicting duplicate manifest for {blob_hash[:16]} from {source_peer[:16]}",
                RNS.LOG_WARNING,
            )
        await _persist_peer_chunk_offer(
            board_id,
            source_peer,
            blob_hash,
            manifest.chunk_count,
            True,
            _full_chunk_ranges(manifest.chunk_count),
        )
        try:
            session.event_loop.call_soon_threadsafe(session.manifest_event.set)
        except RuntimeError:
            session.manifest_event.set()
        return

    reassembly = ReassemblyBuffer(chunk_assembly_path(board_id, blob_hash), manifest.blob_size, manifest.chunk_count)
    reassembly.reserve()
    with session.lock:
        session.manifest = manifest
        session.entries = entries
        session.entries_by_index = {e.chunk_index: e for e in entries}
        session.reassembly = reassembly
    db = await get_board_connection(board_id)
    ref = await get_blob_reference(db, blob_hash)
    await save_chunk_manifest(
        db,
        manifest,
        entries,
        expires_at=int((ref or {}).get("expiry_timestamp", 0)),
    )
    await _persist_peer_chunk_offer(board_id, source_peer, blob_hash, manifest.chunk_count, True, _full_chunk_ranges(manifest.chunk_count))
    existing = await load_chunk_fetch_session(db, session.session_id)
    if existing is None:
        existing = ChunkFetchSession(
            session_id=session.session_id,
            board_id=board_id,
            blob_hash=blob_hash,
            blob_kind=manifest.blob_kind,
            state="manifest_received",
            request_peer_lxmf_hash=session.assigned_peer_lxmf_hash,
        )
    else:
        existing.state = "manifest_received"
        existing.updated_at = int(time.time())
    await save_chunk_fetch_session(db, existing)

    try:
        session.event_loop.call_soon_threadsafe(session.manifest_event.set)
    except RuntimeError:
        session.manifest_event.set()

async def handle_chunk_manifest_unavailable_lxmf(content, source_hash) -> None:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return
    blob_hash = str(payload.get("blob_hash", ""))
    session = get_pending_chunk_session_by_blob(blob_hash)
    if session is None or session.manifest is not None:
        return

    reason = str(payload.get("reason", "not_found"))
    peer_lxmf_hash = source_hash.hex() if isinstance(source_hash, bytes) else str(source_hash)
    with session.lock:
        if peer_lxmf_hash:
            session.manifest_unavailable_by_peer[peer_lxmf_hash] = reason

    # Only treat object-intrinsic hard-stop lifecycle/policy cases as
    # immediately fatal for the whole session. Peer-local withholding and
    # availability failures remain peer-scoped negatives.
    if reason in {"abandoned", "policy_rejected"}:
        session.unavailable_reason = reason
        fail_pending_chunk_session(blob_hash, f"manifest unavailable: {reason}")


async def handle_chunk_offer_lxmf(content, source_hash) -> None:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return
    board_id = str(payload.get("board_id", ""))
    blob_hash = str(payload.get("blob_hash", ""))

    RNS.log(
        f"Received CHUNK_OFFER for {blob_hash[:12]} from "
        f"{source_hash.hex()[:16] if source_hash else 'unknown'}",
        RNS.LOG_DEBUG,
    )
    peer_lxmf_hash = source_hash.hex() if isinstance(source_hash, bytes) else str(source_hash)
    if not board_id or not blob_hash or not peer_lxmf_hash:
        return

    raw_ranges = payload.get("ranges", [])
    ranges: list[tuple[int, int]] =[]
    for item in raw_ranges:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        ranges.append((int(item[0]), int(item[1])))

    chunk_count = int(payload.get("chunk_count", 0))
    complete = bool(payload.get("complete", False))
    await _persist_peer_chunk_offer(board_id, peer_lxmf_hash, blob_hash, chunk_count, complete, ranges)

    session = get_pending_chunk_session_by_blob(blob_hash)
    if session is None or session.board_id != board_id or session.swarm is None:
        return
    with session.lock:
        if session.swarm is not None:
            session.swarm.update_peer_availability(peer_lxmf_hash, ranges)


async def handle_chunk_request_lxmf(content, source_hash, source_identity, sync_engine) -> None:
    try:
        req = json.loads(content)
    except json.JSONDecodeError:
        return
    board_id = str(req.get("board_id", ""))
    blob_hash = str(req.get("blob_hash", ""))
    request_id = str(req.get("request_id", ""))
    chunk_index = int(req.get("chunk_index", -1))

    RNS.log(
        f"Received CHUNK_REQ for {blob_hash[:12]} chunk {chunk_index} from "
        f"{source_hash.hex()[:16] if source_hash else 'unknown'}",
        RNS.LOG_DEBUG,
    )
    peer_lxmf_hash = source_hash.hex() if isinstance(source_hash, bytes) else str(source_hash)
    if not board_id or not blob_hash or not request_id or chunk_index < 0:
        return
    if source_identity is None:
        RNS.log(
            f"Dropping CHUNK_REQUEST {request_id[:16]} from {peer_lxmf_hash[:16]}: missing source identity",
            RNS.LOG_WARNING,
        )
        return
    RNS.log(
        f"Received CHUNK_REQUEST via LXMF from {peer_lxmf_hash[:16]} "
        f"for {blob_hash[:16]} chunk {chunk_index}",
        RNS.LOG_DEBUG,
    )
    _track_interested_peer(blob_hash, peer_lxmf_hash)

    db = await get_board_connection(board_id)
    decision = await should_serve_blob(db, blob_hash)
    if not decision.allowed:
        return
    live_chunk = _read_live_verified_chunk(board_id, blob_hash, chunk_index)
    if live_chunk is not None:
        manifest, data = live_chunk
        session = get_pending_chunk_session_by_blob(blob_hash)
        ranges: list[tuple[int, int]] = []
        complete = False
        if session is not None:
            with session.lock:
                ranges = _compress_chunk_indexes(session.stored_chunks)
                complete = (
                    manifest.chunk_count > 0
                    and len(session.stored_chunks) >= manifest.chunk_count
                )
        await _send_chunk_offer(
            sync_engine,
            peer_lxmf_hash,
            board_id,
            blob_hash,
            manifest.chunk_count,
            complete,
            ranges,
        )
        _enqueue_payload_resource_send(
            source_identity,
            data,
            {
                "board_id": board_id,
                "blob_hash": blob_hash,
                "chunk_index": chunk_index,
                "request_id": request_id,
                "peer_lxmf_hash": sync_engine.get_lxmf_hash() or peer_lxmf_hash,
            },
        )
        RNS.log(
            f"Queued live partial chunk send to {peer_lxmf_hash[:16]} "
            f"for {blob_hash[:16]} chunk {chunk_index}",
            RNS.LOG_DEBUG,
        )
        return
    loaded = await load_chunk_manifest(db, blob_hash)
    if loaded is None:
        ref = await get_blob_reference(db, blob_hash)
        blob = read_payload(board_id, blob_hash)
        if ref is None or blob is None:
            return
        manifest, entries = build_chunk_manifest(
            board_id=board_id,
            post_id=ref["post_id"],
            thread_id=ref["thread_id"],
            blob_kind=ref["blob_kind"],
            blob=blob,
            chunk_size=_choose_chunk_size(len(blob)),
        )
        await save_chunk_manifest(db, manifest, entries, expires_at=int(ref.get("expiry_timestamp", 0)))
    else:
        manifest, entries = loaded

    if chunk_index < 0 or chunk_index >= len(entries):
        return
    entry = entries[chunk_index]
    blob = read_payload(board_id, blob_hash)
    if blob is None:
        return
    data = blob[entry.offset: entry.offset + entry.size]
    local_ranges = _full_chunk_ranges(manifest.chunk_count)
    await _send_chunk_offer(sync_engine, peer_lxmf_hash, board_id, blob_hash, manifest.chunk_count, True, local_ranges)
    _enqueue_payload_resource_send(
        source_identity,
        data,
        {
            "board_id": board_id,
            "blob_hash": blob_hash,
            "chunk_index": chunk_index,
            "request_id": request_id,
            "peer_lxmf_hash": sync_engine.get_lxmf_hash() or peer_lxmf_hash,
        },
    )
    RNS.log(
        f"Queued chunk payload send to {peer_lxmf_hash[:16]} "
        f"for {blob_hash[:16]} chunk {chunk_index}",
        RNS.LOG_DEBUG,
    )

async def handle_chunk_cancel_lxmf(content, source_hash, source_identity, sync_engine) -> None:
    try:
        req = json.loads(content)
    except json.JSONDecodeError:
        return
    request_id = str(req.get("request_id", ""))

    RNS.log(
        f"Received CHUNK_CANCEL for request {request_id[:8]} from "
        f"{source_hash.hex()[:16] if source_hash else 'unknown'}",
        RNS.LOG_DEBUG,
    )
    if not request_id:
        return
    res = None
    with _payload_sender_lock:
        _cancelled_outbound_chunk_requests.add(request_id)
        res = _active_outbound_chunk_resources.get(request_id)
    if res:
        try:
            res.cancel()
        except Exception:
            pass


# =========================================================================
# Legacy handler
# =========================================================================

def _estimate_fetch_timeout(expected_size: int | None, base_timeout: float | None = None) -> float:
    floor = _FETCH_TIMEOUT_DEFAULT if not base_timeout or base_timeout <= 0 else max(base_timeout, _FETCH_TIMEOUT_MIN)
    if not expected_size or expected_size <= 0:
        return min(_FETCH_TIMEOUT_MAX, floor)
    estimated = expected_size / _FETCH_TIMEOUT_BYTES_PER_SEC
    return min(_FETCH_TIMEOUT_MAX, max(floor, estimated))


def _choose_chunk_size(blob_size: int) -> int:
    if blob_size <= 0:
        return _CHUNK_SIZE_DEFAULT
    if blob_size < (64 * 1024):
        return 32 * 1024
    if blob_size < (512 * 1024):
        return 64 * 1024
    return _CHUNK_SIZE_DEFAULT


def _control_priority():
    from retiboard.sync.message_queue import Priority
    return Priority.CONTROL


def _compress_chunk_indexes(indexes: set[int] | list[int]) -> list[tuple[int, int]]:
    ordered = sorted({int(idx) for idx in indexes if int(idx) >= 0})
    if not ordered:
        return []
    ranges: list[tuple[int, int]] =[]
    start = prev = ordered[0]
    for idx in ordered[1:]:
        if idx == prev + 1:
            prev = idx
            continue
        ranges.append((start, prev))
        start = prev = idx
    ranges.append((start, prev))
    return ranges


def _full_chunk_ranges(chunk_count: int) -> list[tuple[int, int]]:
    return[(0, chunk_count - 1)] if chunk_count > 0 else[]


async def _get_local_chunk_offer(board_id: str, blob_hash: str):
    session = get_pending_chunk_session_by_blob(blob_hash)
    if session is not None and session.manifest is not None:
        ranges = _compress_chunk_indexes(session.stored_chunks)
        complete = session.manifest.chunk_count > 0 and len(session.stored_chunks) >= session.manifest.chunk_count
        return session.manifest, session.entries, ranges, complete

    db = await get_board_connection(board_id)
    decision = await should_serve_blob(db, blob_hash)
    if not decision.allowed:
        return None, None,[], False
    loaded = await load_chunk_manifest(db, blob_hash)
    if loaded is not None and payload_exists(board_id, blob_hash):
        manifest, entries = loaded
        return manifest, entries, _full_chunk_ranges(manifest.chunk_count), True

    if payload_exists(board_id, blob_hash):
        ref = await get_blob_reference(db, blob_hash)
        if ref is None:
            return None, None,[], False
        payload_data = read_payload(board_id, blob_hash)
        if payload_data is None:
            return None, None,[], False
        blob_kind = str(ref.get("blob_kind") or "text")
        manifest, entries = build_chunk_manifest(
            board_id=board_id,
            post_id=str(ref.get("post_id") or ""),
            thread_id=str(ref.get("thread_id") or ""),
            blob_kind=blob_kind,
            blob=payload_data,
            chunk_size=_choose_chunk_size(len(payload_data)),
        )
        await save_chunk_manifest(db, manifest, entries, expires_at=int(ref.get("expires_at") or 0))
        return manifest, entries, _full_chunk_ranges(manifest.chunk_count), True

    return None, None,[], False

async def _persist_peer_chunk_offer(board_id: str, peer_lxmf_hash: str, blob_hash: str, chunk_count: int, complete: bool, ranges: list[tuple[int, int]]) -> None:
    db = await get_board_connection(board_id)
    await upsert_peer_chunk_availability(
        db,
        board_id=board_id,
        peer_lxmf_hash=peer_lxmf_hash,
        blob_hash=blob_hash,
        chunk_count=chunk_count,
        complete=complete,
        ranges=ranges,
    )

async def _wait_any(events: list[asyncio.Event], timeout: float) -> Optional[asyncio.Event]:
    tasks =[asyncio.create_task(evt.wait()) for evt in events]
    try:
        done, pending = await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        pending = [task for task in tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    if not done:
        return None
    winner = next(iter(done))
    idx = tasks.index(winner)
    return events[idx]


async def fetch_payload_from_peers(
    board_id,
    content_hash,
    peer_tracker,
    self_lxmf_hash="",
    sync_engine=None,
    timeout=60.0,
    expected_size: int | None = None,
    manual_override: bool = False,
) -> bool:
    if payload_exists(board_id, content_hash):
        return True

    if sync_engine is None:
        return False

    with _legacy_fetch_lock:
        _active_legacy_fetches.add(content_hash)

    try:
        if expected_size and expected_size >= _CHUNK_FETCH_THRESHOLD:
            ok = await fetch_payload_from_peers_chunked(
                board_id,
                content_hash,
                peer_tracker,
                self_lxmf_hash=self_lxmf_hash,
                sync_engine=sync_engine,
                expected_size=expected_size,
                manual_override=manual_override,
            )
            if ok is _CHUNK_FETCH_RESULT_PAUSED:
                return False
            if ok:
                return True

        # Fallback legacy routine for small unchunked payload requests
        peers = peer_tracker.get_fetch_peers(board_id)
        if self_lxmf_hash:
            peers =[p for p in peers if p.lxmf_hash != self_lxmf_hash]
        if not peers:
            return False

        announce_payload_destination()

        loop = asyncio.get_running_loop()
        evt = register_pending_fetch(content_hash, board_id, loop)

        request_payload = json.dumps({
            "board_id": board_id,
            "content_hash": content_hash,
        }, separators=(",", ":")).encode("utf-8")

        from retiboard.sync.message_queue import Priority

        async def _dispatch_requests() -> int:
            sent = 0
            for idx, peer in enumerate(peers):
                if payload_exists(board_id, content_hash):
                    break
                if peer.identity is None:
                    continue
                if idx > 0:
                    await asyncio.sleep(min(1.5 * idx, 3.0))
                    if payload_exists(board_id, content_hash):
                        break
                result = sync_engine.send_lxmf(
                    peer.lxmf_hash,
                    request_payload,
                    MSG_TYPE_PAYLOAD_REQ,
                    Priority.DATA,
                )
                if result:
                    sent += 1
            return sent

        sent_count = await _dispatch_requests()
        if sent_count == 0:
            cancel_pending_fetch(content_hash)
            return False

        effective_timeout = _estimate_fetch_timeout(expected_size, timeout)
        started_at = time.time()

        try:
            while True:
                if payload_exists(board_id, content_hash):
                    break
                elapsed = time.time() - started_at
                remaining = effective_timeout - elapsed
                if remaining <= 0:
                    raise asyncio.TimeoutError
                wait_slice = min(_FETCH_TIMEOUT_POLL_INTERVAL, remaining)
                try:
                    await asyncio.wait_for(evt.wait(), timeout=wait_slice)
                    break
                except asyncio.TimeoutError:
                    continue
        except asyncio.TimeoutError:
            cancel_pending_fetch(content_hash)

        return payload_exists(board_id, content_hash)
    finally:
        with _legacy_fetch_lock:
            _active_legacy_fetches.discard(content_hash)


async def _restore_chunk_session_state(session: PendingChunkSession) -> bool:
    db = await get_board_connection(session.board_id)
    persisted = await load_latest_chunk_fetch_session_for_blob(
        db,
        board_id=session.board_id,
        blob_hash=session.blob_hash,
    )
    if persisted is None:
        return False
    with session.lock:
        session.session_id = persisted.session_id
    loaded = await load_chunk_manifest(db, session.blob_hash)
    if loaded is None:
        return False
    manifest, entries = loaded
    assembly_path = chunk_assembly_path(session.board_id, session.blob_hash)
    reassembly = ReassemblyBuffer(assembly_path, manifest.blob_size, manifest.chunk_count)
    if not assembly_path.exists() or assembly_path.stat().st_size != manifest.blob_size:
        reassembly.reserve()
    with session.lock:
        session.manifest = manifest
        session.entries = entries
        session.entries_by_index = {e.chunk_index: e for e in entries}
        session.reassembly = reassembly
    states = await load_chunk_request_states(db, session.session_id)
    verified_count = 0
    corrupted_count = 0
    for row in states:
        chunk_idx = row.chunk_index
        if row.state != "stored":
            continue
        with session.lock:
            entry = session.entries_by_index.get(chunk_idx)
            reassembly_obj = session.reassembly
        if reassembly_obj is None or entry is None:
            corrupted_count += 1
            continue
        if reassembly_obj.verify_chunk_on_disk(chunk_idx, entry.offset, entry.size, entry.chunk_hash):
            with session.lock:
                session.stored_chunks.add(chunk_idx)
                reassembly_obj.mark_present(chunk_idx)
            verified_count += 1
            continue
        await save_chunk_request_state(
            db,
            ChunkRequestStateRecord(
                session_id=session.session_id,
                chunk_index=chunk_idx,
                state="missing",
                assigned_peer_lxmf_hash="",
                request_id="",
                attempt_count=int(row.attempt_count),
                deadline_at=0,
                updated_at=int(time.time()),
            ),
        )
        corrupted_count += 1
    if corrupted_count > 0:
        RNS.log(
            f"Chunk session restore for {session.blob_hash[:16]}: "
            f"{verified_count} verified, {corrupted_count} corrupted/missing - "
            "will re-fetch corrupted chunks",
            RNS.LOG_WARNING,
        )
    with session.lock:
        session.resumed_from_persisted = True
        session.paused = persisted.state == "paused"
    return True

async def _drain_pending_chunk_cancels(session: PendingChunkSession, sync_engine) -> None:
    while True:
        with session.lock:
            if not session.pending_cancels:
                return
            peer_lxmf_hash, chunk_index, request_id = session.pending_cancels.pop(0)
        payload = json.dumps(
            {
                "board_id": session.board_id,
                "blob_hash": session.blob_hash,
                "chunk_index": int(chunk_index),
                "request_id": request_id,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        sync_engine.send_lxmf(peer_lxmf_hash, payload, MSG_TYPE_CHUNK_CANCEL, _control_priority())


def _has_live_chunk_session_state(session: PendingChunkSession) -> bool:
    """Return True if the in-memory session can resume without a manifest round-trip."""
    with session.lock:
        manifest = session.manifest
        reassembly = session.reassembly
        entries = session.entries
        entries_by_index = session.entries_by_index
    return bool(
        manifest is not None
        and reassembly is not None
        and entries
        and entries_by_index
    )


def _seed_swarm_from_session_state(
    session: PendingChunkSession,
    prior_states: list[ChunkRequestStateRecord],
) -> None:
    """Apply known stored chunks and attempt counts to a fresh swarm."""
    if session.swarm is None:
        return

    stored_indexes: set[int] = set()
    with session.lock:
        stored_indexes.update(int(idx) for idx in session.stored_chunks)

    for row in prior_states:
        chunk = session.swarm.chunks.get(row.chunk_index)
        if chunk is None:
            continue
        chunk.attempt_count = max(chunk.attempt_count, int(row.attempt_count))
        if row.state == "stored":
            stored_indexes.add(int(row.chunk_index))

    for chunk_index in stored_indexes:
        chunk = session.swarm.chunks.get(chunk_index)
        if chunk is None:
            continue
        chunk.stored = True
        chunk.state = ChunkFetchState.STORED


def _peer_has_positive_chunk_availability(row: dict | None) -> bool:
    if not row:
        return False
    if bool(row.get("complete")):
        return True
    ranges = row.get("ranges") or []
    return any(int(end) >= int(start) for start, end in ranges)


def _select_swarm_peers(
    candidate_peer_hashes: list[str],
    availability_rows: dict[str, dict],
    *,
    prefer_known_available: bool,
) -> tuple[list[str], dict[str, list[tuple[int, int]]]]:
    candidate_set = set(candidate_peer_hashes)
    peer_ranges = {
        peer_hash: row.get("ranges", [])
        for peer_hash, row in availability_rows.items()
        if peer_hash in candidate_set
    }
    if not prefer_known_available:
        return list(candidate_peer_hashes), peer_ranges

    eligible = [
        peer_hash
        for peer_hash in candidate_peer_hashes
        if _peer_has_positive_chunk_availability(availability_rows.get(peer_hash))
    ]
    if not eligible:
        return list(candidate_peer_hashes), peer_ranges

    filtered_ranges = {peer_hash: peer_ranges.get(peer_hash, []) for peer_hash in eligible}
    return eligible, filtered_ranges


async def fetch_payload_from_peers_chunked(
    board_id: str,
    blob_hash: str,
    peer_tracker,
    *,
    self_lxmf_hash: str = "",
    sync_engine=None,
    expected_size: int | None = None,
    manual_override: bool = False,
) -> bool | object:
    if payload_exists(board_id, blob_hash):
        return True
    if sync_engine is None:
        return False

    peers = peer_tracker.get_fetch_peers(board_id)
    if self_lxmf_hash:
        peers =[p for p in peers if p.lxmf_hash != self_lxmf_hash]
    peers =[p for p in peers if p.identity is not None]
    if not peers:
        return False

    announce_payload_destination()
    loop = asyncio.get_running_loop()
    existing_db = await get_board_connection(board_id)
    existing = await load_latest_chunk_fetch_session_for_blob(existing_db, board_id=board_id, blob_hash=blob_hash)
    blob_ref = await get_blob_reference(existing_db, blob_hash)
    blob_kind = str((blob_ref or {}).get("blob_kind") or (existing.blob_kind if existing is not None else "text"))
    session = register_pending_chunk_session(
        board_id,
        blob_hash,
        blob_kind,
        peers[0].lxmf_hash,
        loop,
        session_id=existing.session_id if existing is not None else None,
    )
    
    with session.lock:
        session.candidate_peer_lxmf_hashes = [peer.lxmf_hash for peer in peers]
        session.sync_engine = sync_engine
        
        # If this is a manual request, ensure we are not paused.
        if manual_override and getattr(session, "paused", False):
            session.paused = False
            session.last_error = ""

        # Apply scheduling constraints and priority upgrades
        if manual_override and not session.manual_override:
            session.manual_override = True
            decision = get_payload_scheduler().classify(
                blob_kind=session.blob_kind,
                expected_size=int(expected_size or 0),
                manual_override=True
            )
            session.priority_class = decision.priority_class
            get_payload_scheduler().register_session(
                session_id=session.session_id,
                blob_hash=blob_hash,
                blob_kind=session.blob_kind,
                expected_size=int(expected_size or 0),
                manual_override=True
            )
        else:
            decision = get_payload_scheduler().register_session(
                session_id=session.session_id,
                blob_hash=blob_hash,
                blob_kind=blob_kind,
                expected_size=int(expected_size or 0),
                manual_override=manual_override,
            )
            session.manual_override = bool(manual_override)
            session.priority_class = decision.priority_class

        session.batcher = session.batcher or ChunkStateBatcher(board_id=board_id)

    # Prevent concurrent swarm loops acting on the same session in-memory
    while True:
        already_running = False
        with session.lock:
            if getattr(session, 'is_running', False):
                already_running = True
            else:
                session.is_running = True
                
        if not already_running:
            break
            
        # It is running. Wait for it to gracefully exit.
        while True:
            with session.lock:
                if not getattr(session, 'is_running', False):
                    break
            await asyncio.sleep(0.1)
            
        # Once it stopped, if the payload exists we are done
        if payload_exists(board_id, blob_hash):
            return True
        # Otherwise loop around and try to acquire it

    success = False
    try:
        restored = await _restore_chunk_session_state(session)
        warm_resumed = False
        if not restored and _has_live_chunk_session_state(session):
            warm_resumed = True
            with session.lock:
                session.resumed_from_persisted = bool(session.stored_chunks)
        if existing is None:
            db = await get_board_connection(board_id)
            await save_chunk_fetch_session(
                db,
                ChunkFetchSession(
                    session_id=session.session_id,
                    board_id=board_id,
                    blob_hash=blob_hash,
                    blob_kind=session.blob_kind,
                    state="started",
                    request_peer_lxmf_hash=peers[0].lxmf_hash,
                ),
            )

        manifest_ready = restored or warm_resumed or await _request_chunk_manifest(session, peers, sync_engine)
        if not manifest_ready or session.manifest is None:
            return payload_exists(board_id, blob_hash)

        timeout_for_index = {
            entry.chunk_index: _next_chunk_timeout(entry.size, is_low_bandwidth())
            for entry in session.entries
        }
        db = await get_board_connection(board_id)
        availability_rows = await load_peer_chunk_availability(db, board_id=board_id, blob_hash=blob_hash)
        swarm_peer_hashes, peer_ranges = _select_swarm_peers(
            session.candidate_peer_lxmf_hashes,
            availability_rows,
            prefer_known_available=bool(restored or warm_resumed),
        )
        if swarm_peer_hashes != session.candidate_peer_lxmf_hashes:
            RNS.log(
                f"Warm resume for {blob_hash[:16]} narrowed swarm peers "
                f"from {len(session.candidate_peer_lxmf_hashes)} to {len(swarm_peer_hashes)} "
                "based on known chunk availability",
                RNS.LOG_DEBUG,
            )
        priority_mode = PriorityMode.RAREST_FIRST if any(peer_ranges.values()) else PriorityMode.HYBRID
        session.swarm = SwarmFetcher(
            peer_lxmf_hashes=swarm_peer_hashes,
            chunk_count=session.manifest.chunk_count,
            next_chunk_timeout=lambda idx: timeout_for_index.get(idx, _CHUNK_REQUEST_TIMEOUT),
            max_attempts_per_chunk=6,
            priority_mode=priority_mode,
            peer_chunk_ranges=peer_ranges,
            is_low_bandwidth=is_low_bandwidth(),
        )
        penalties = await load_chunk_peer_penalties(db, board_id=board_id, peer_lxmf_hashes=swarm_peer_hashes)
        for peer_hash, record in penalties.items():
            session.swarm.apply_persisted_peer_state(
                peer_hash,
                timeout_count=record.timeout_count,
                invalid_chunk_count=record.invalid_chunk_count,
                success_count=record.success_count,
                cooldown_until=record.cooldown_until,
            )
        if restored or warm_resumed:
            prior_states = await load_chunk_request_states(db, session.session_id)
            _seed_swarm_from_session_state(session, prior_states)

        success = await _fetch_manifest_chunks_swarm(session, sync_engine)
        if success is _CHUNK_FETCH_RESULT_PAUSED:
            return _CHUNK_FETCH_RESULT_PAUSED
        return bool(success and payload_exists(board_id, blob_hash))
    except Exception as exc:
        RNS.log(f"Chunked payload fetch crashed for {blob_hash[:16]}: {exc}", RNS.LOG_ERROR)
        return False
    finally:
        with session.lock:
            session.is_running = False
            paused = getattr(session, 'paused', False)
        
        if session.batcher is not None:
            try:
                await session.batcher.flush()
            except Exception as exc:
                RNS.log(f"Failed final batch flush for {blob_hash[:16]}: {exc}", RNS.LOG_WARNING)
        
        if success is not _CHUNK_FETCH_RESULT_PAUSED and not success and not payload_exists(board_id, blob_hash) and not paused:
            await cancel_pending_chunk_session(blob_hash)

async def _request_chunk_manifest(session: PendingChunkSession, peers, sync_engine) -> bool:
    session.manifest_event.clear()
    session.failed_event.clear()
    with session.lock:
        session.unavailable_reason = ""
        session.manifest_unavailable_by_peer.clear()
    req = json.dumps({"board_id": session.board_id, "blob_hash": session.blob_hash}, separators=(",", ":")).encode("utf-8")
    sent = 0
    queued = 0
    for peer in peers:
        result = sync_engine.send_lxmf(peer.lxmf_hash, req, MSG_TYPE_CHUNK_MANIFEST_REQ, _control_priority())
        if result == SendResult.SENT:
            sent += 1
        elif result == SendResult.QUEUED:
            queued += 1

    if sent == 0 and queued == 0:
        return False

    # Dynamic timeout: more time if many requests are queued (§14.4).
    base_timeout = _CHUNK_MANIFEST_TIMEOUT
    if queued > 0:
        base_timeout += 15.0 # Extra 15s for path resolution
    
    deadline = time.time() + base_timeout
    candidate_peers = {peer.lxmf_hash for peer in peers if getattr(peer, "lxmf_hash", "")}
    
    RNS.log(
        f"Manifest request for {session.blob_hash[:16]} "
        f"({sent} sent, {queued} queued), "
        f"timeout {base_timeout:.1f}s",
        RNS.LOG_DEBUG,
    )

    while time.time() < deadline:
        with session.lock:
            manifest_ready = session.manifest is not None
        if manifest_ready:
            # Brief sleep to let any following messages arrive.
            await asyncio.sleep(0.35)
            return True

        # Wait for any of the events.
        which = await _wait_any(
            [session.manifest_event, session.failed_event, session.chunk_event], 
            min(1.0, max(0.1, deadline - time.time()))
        )
        
        with session.lock:
            manifest_after_wait = session.manifest is not None
            unavailable_reason = session.unavailable_reason
            unavailable_by_peer = dict(session.manifest_unavailable_by_peer)
            
        if which is session.manifest_event and manifest_after_wait:
            await asyncio.sleep(0.35)
            return True

        # If a path was discovered (signaled via chunk_event), log and continue.
        if which is session.chunk_event:
            RNS.log(f"Path resolved for manifest request {session.blob_hash[:16]}", RNS.LOG_DEBUG)
            continue

        if which is session.failed_event and unavailable_reason in {"abandoned", "policy_rejected"}:
            return False

        if candidate_peers and candidate_peers.issubset(set(unavailable_by_peer.keys())):
            reasons = set(unavailable_by_peer.values())
            # (rest of reason logic remains same)
            if "abandoned" in reasons:
                with session.lock:
                    session.unavailable_reason = "abandoned"
            elif "policy_rejected" in reasons:
                with session.lock:
                    session.unavailable_reason = "policy_rejected"
            elif "withheld_local_policy" in reasons:
                with session.lock:
                    session.unavailable_reason = "withheld_local_policy"
            elif "pruned" in reasons:
                with session.lock:
                    session.unavailable_reason = "pruned"
            else:
                with session.lock:
                    session.unavailable_reason = "not_found"
            return False

    with session.lock:
        return session.manifest is not None


def _send_chunk_request_immediate(sync_engine, peer_lxmf_hash: str, payload: bytes) -> SendResult:
    """Send chunk requests only when the peer path is immediately usable."""
    if sync_engine is None:
        return SendResult.REJECTED

    peer_tracker = getattr(sync_engine, "peer_tracker", None)
    peer = peer_tracker.get_peer(peer_lxmf_hash) if peer_tracker is not None else None
    if peer is None or getattr(peer, "identity", None) is None:
        return SendResult.REJECTED

    if getattr(peer, "path_state", None) == PathState.UNREACHABLE:
        next_retry_at = float(getattr(peer, "next_retry_at", 0.0) or 0.0)
        if time.time() < next_retry_at:
            return SendResult.REJECTED

    # v3.6.4: Avoid spamming RNS path requests if one is already in-flight.
    # Steady-state path discovery is handled by SyncEngine._path_resolution_loop().
    if getattr(peer, "path_state", None) != PathState.KNOWN:
        if getattr(peer, "path_state", None) == PathState.REQUESTED:
            # Already requested; don't spam.
            return SendResult.QUEUED
            
        request_path = getattr(sync_engine, "_request_path", None)
        if callable(request_path):
            request_path(peer_lxmf_hash)
        return SendResult.QUEUED

    try:
        dest_hash = bytes.fromhex(peer_lxmf_hash)
    except ValueError:
        return SendResult.REJECTED

    if not RNS.Transport.has_path(dest_hash):
        if peer_tracker is not None:
            peer_tracker.record_delivery_failure(peer_lxmf_hash)
        request_path = getattr(sync_engine, "_request_path", None)
        if callable(request_path):
            request_path(peer_lxmf_hash)
        return SendResult.QUEUED

    try_send = getattr(sync_engine, "_try_send_lxmf", None)
    if not callable(try_send):
        return SendResult.REJECTED

    if try_send(peer, payload, MSG_TYPE_CHUNK_REQ):
        if peer_tracker is not None:
            peer_tracker.mark_path_known(peer_lxmf_hash)
        return SendResult.SENT

    if peer_tracker is not None:
        peer_tracker.record_delivery_failure(peer_lxmf_hash)
    request_path = getattr(sync_engine, "_request_path", None)
    if callable(request_path):
        request_path(peer_lxmf_hash)
    return SendResult.QUEUED


async def _fetch_manifest_chunks_swarm(session: PendingChunkSession, sync_engine) -> bool | object:
    if session.swarm is None:
        return False

    idle_rounds = 0
    while True:
        if session.batcher is not None and session.batcher.should_flush():
            await session.batcher.flush()
        await _drain_pending_chunk_cancels(session, sync_engine)
        if payload_exists(session.board_id, session.blob_hash) or session.finalized:
            return True
        with session.lock:
            paused = session.paused
            cancel_requested = session.cancel_requested
        if cancel_requested:
            session.last_error = "Download cancelled"
            get_payload_scheduler().release_session_requests(session.session_id)
            return False
        if paused:
            session.last_error = ""  # Ensure we don't display fake errors while paused.
            if session.batcher is not None:
                await session.batcher.flush()
            get_payload_scheduler().release_session_requests(session.session_id)
            return _CHUNK_FETCH_RESULT_PAUSED
        if session.failed_event.is_set():
            get_payload_scheduler().release_session_requests(session.session_id)
            return False

        session.chunk_event.clear()
        session.completed_event.clear()
        with session.lock:
            session.swarm.process_timeouts()
            expired_requests = session.swarm.take_recent_timeouts()
            timeout_records =[]
            for expired in expired_requests:
                chunk_state = session.swarm.chunks.get(expired.chunk_index)
                attempt_count = chunk_state.attempt_count if chunk_state is not None else 0
                timeout_records.append((expired, attempt_count))
            plans = session.swarm.plan_requests()
        for expired, attempt_count in timeout_records:
            get_payload_scheduler().release_request(expired.request_id)
            _queue_chunk_state(
                session,
                chunk_index=expired.chunk_index,
                state="timed_out",
                assigned_peer_lxmf_hash=expired.peer_lxmf_hash,
                request_id=expired.request_id,
                attempt_count=attempt_count,
                deadline_at=int(expired.deadline_at),
            )
            _queue_swarm_peer_state(session, expired.peer_lxmf_hash)
        sent_this_round = 0
        scheduler = get_payload_scheduler()
        for plan in plans:
            if not scheduler.try_acquire_request(session.session_id, plan.request_id):
                continue
            payload = json.dumps(
                {
                    "board_id": session.board_id,
                    "blob_hash": session.blob_hash,
                    "chunk_index": plan.chunk_index,
                    "request_id": plan.request_id,
                },
                separators=(",", ":"),
            ).encode("utf-8")
            result = _send_chunk_request_immediate(sync_engine, plan.peer_lxmf_hash, payload)
            if result == SendResult.SENT:
                with session.lock:
                    session.swarm.mark_request_sent(plan.request_id)
                    request = session.swarm.lookup_request(plan.request_id)
                    chunk_state = session.swarm.chunks.get(plan.chunk_index)
                    attempt_count = chunk_state.attempt_count if chunk_state is not None else 0
                    deadline_at = int(request.deadline_at) if request is not None else 0
                _queue_chunk_state(
                    session,
                    chunk_index=plan.chunk_index,
                    state="requested",
                    assigned_peer_lxmf_hash=plan.peer_lxmf_hash,
                    request_id=plan.request_id,
                    attempt_count=attempt_count,
                    deadline_at=deadline_at,
                )
                sent_this_round += 1
            elif result == SendResult.QUEUED:
                scheduler.release_request(plan.request_id)
                with session.lock:
                    session.swarm.mark_request_deferred(
                        plan.request_id,
                        state=ChunkFetchState.REQUEST_ENQUEUED,
                    )
                    chunk_state = session.swarm.chunks.get(plan.chunk_index)
                    attempt_count = chunk_state.attempt_count if chunk_state is not None else 0
                _queue_chunk_state(
                    session,
                    chunk_index=plan.chunk_index,
                    state="request_enqueued",
                    assigned_peer_lxmf_hash=plan.peer_lxmf_hash,
                    request_id="",
                    attempt_count=attempt_count,
                    deadline_at=0,
                )
            else:
                scheduler.release_request(plan.request_id)
                with session.lock:
                    session.swarm.mark_request_deferred(
                        plan.request_id,
                        state=ChunkFetchState.MISSING,
                    )
                    chunk_state = session.swarm.chunks.get(plan.chunk_index)
                    attempt_count = chunk_state.attempt_count if chunk_state is not None else 0
                _queue_chunk_state(
                    session,
                    chunk_index=plan.chunk_index,
                    state="missing",
                    assigned_peer_lxmf_hash="",
                    request_id="",
                    attempt_count=attempt_count,
                    deadline_at=0,
                )

        if payload_exists(session.board_id, session.blob_hash) or session.finalized:
            if session.batcher is not None:
                await session.batcher.flush()
            get_payload_scheduler().release_session_requests(session.session_id)
            return True

        with session.lock:
            active_request_count = session.swarm.active_request_count()
            can_make_progress = session.swarm.can_make_progress()
            # v3.6.3: Transition to finalizing state if we have all chunks
            # but haven't written the final blob yet.
            if session.swarm.is_complete() and not session.finalized:
                session.state = "finalizing"
            
            # v3.6.3: Count REQUEST_ENQUEUED (waiting for path) as active work
            # to prevent the idle-out timer from firing during slow path discovery.
            has_pending_paths = any(
                c.state == ChunkFetchState.REQUEST_ENQUEUED 
                for c in session.swarm.chunks.values()
            )

        if sent_this_round == 0 and active_request_count == 0:
            if has_pending_paths:
                # If we are waiting for paths, the loop should spin slower to save CPU/battery.
                await asyncio.sleep(0.5)
                
            # v3.6.4: Do NOT reset idle_rounds to 0 if we are only waiting for paths.
            # This allows the loop to eventually timeout if paths cannot be resolved,
            # preventing the session from hanging at 0% indefinitely.
            idle_rounds += 1
        else:
            idle_rounds = 0

        # Increased from 6 to 40 (~30 seconds) to accommodate slow LoRa path resolution.
        if not can_make_progress or idle_rounds > 40:
            if session.batcher is not None:
                await session.batcher.flush()
            get_payload_scheduler().release_session_requests(session.session_id)
            return False

        which = await _wait_any([session.chunk_event, session.completed_event, session.failed_event], 0.75)
        if which is session.completed_event:
            if session.batcher is not None:
                await session.batcher.flush()
            get_payload_scheduler().release_session_requests(session.session_id)
            return True
        if which is session.failed_event:
            if session.batcher is not None:
                await session.batcher.flush()
            get_payload_scheduler().release_session_requests(session.session_id)
            return False

def handle_payload_response_lxmf(content, source_hash):
    """Legacy placeholder. Whole-payload transfer uses RNS Resource, not LXMF."""
    RNS.log("Ignoring legacy PAYLOAD_RESPONSE over LXMF", RNS.LOG_DEBUG)
