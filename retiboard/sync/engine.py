"""
Gossip Sync Engine — the orchestrator.

Manages the full §7 three-tier sync lifecycle:
  1. Initializes LXMF router and registers delivery callbacks (Tier 1)
  2. Runs periodic HAVE broadcast loop (Tier 2)
  3. Processes queued DELTA_REQUESTs (Tier 3)
  4. Registers RNS request handlers for delta/payload serving

Integrates with:
  - Phase 1 storage layer (db + payloads)
  - Phase 3 PoW verification (via receiver.py)
  - Phase 4 pruning (abandoned threads excluded from all gossip)
  - Phase 2 board announces (HAVE piggybacked on announces)

Startup integration:
  - Created by main.py after BoardManager
  - Background tasks started via FastAPI lifespan or standalone asyncio
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from typing import Optional

import RNS

try:
    import LXMF
    HAS_LXMF = True
except ImportError:
    HAS_LXMF = False
    LXMF = None

from retiboard.config import (
    BOARDS_DIR,
    RETIBOARD_HOME,
)
from retiboard.transport import is_low_bandwidth
from retiboard.sync.peers import PeerTracker, PathState
from retiboard.sync.rate_limiter import SyncRateLimiter
from retiboard.sync.message_queue import (
    MessageQueueManager,
    Priority,
    SendResult,
    QUEUE_EVICTION_SWEEP_INTERVAL,
)
from retiboard.sync.have import build_have_packet, serialize_have, get_have_interval
from retiboard.sync.have_handler import set_sync_engine
from retiboard.sync.delta import delta_request_handler
from retiboard.sync import PATH_DELTA


# Path to persisted peer table.
PATH_PEERS_JSON = RETIBOARD_HOME / "peers.json"


@dataclass
class DeltaRequest:
    """Queued delta request."""
    board_id: str
    thread_id: str
    since_timestamp: int
    known_post_count: int
    target_hash: Optional[bytes] = None
    target_identity: Optional[RNS.Identity] = None
    enqueued_at: float = field(default_factory=time.time)


class SyncEngine:
    """
    Central gossip synchronization engine.

    Lifecycle:
        engine = SyncEngine(identity, board_manager)
        await engine.start()    # Initializes LXMF, registers handlers
        # ... runs background tasks ...
        await engine.stop()     # Cleanup
    """

    def __init__(self, identity: RNS.Identity, board_manager):
        """
        Args:
            identity: This node's RNS identity.
            board_manager: The BoardManager from Phase 2.
        """
        self._identity = identity
        self._board_manager = board_manager

        # Peer tracking (5d).
        self.peer_tracker = PeerTracker()

        # Rate limiting — transport-aware (§7.2, §14.4).
        # is_low_bandwidth() is called at construction time. If the
        # transport changes later (e.g., LoRa interface comes online),
        # the rate limiter's limit is fixed. This is acceptable because
        # the rate limiter uses the conservative (LoRa) limit if ANY
        # slow interface was present at startup. A full restart picks
        # up the new interface state.
        self.rate_limiter = SyncRateLimiter(is_low_bandwidth=is_low_bandwidth())

        # v3.6.2 §8: Per-peer message queue with priority tiers.
        self.message_queue = MessageQueueManager()

        # LXMF router and delivery destination.
        self._lxm_router = None
        self._lxmf_destination = None

        # Delta request queue.
        self._delta_queue: asyncio.Queue[DeltaRequest] = asyncio.Queue()

        # Background task references.
        self._have_task: Optional[asyncio.Task] = None
        self._delta_task: Optional[asyncio.Task] = None
        self._lxmf_announce_task: Optional[asyncio.Task] = None
        self._lxmf_startup_burst_task: Optional[asyncio.Task] = None
        self._queue_sweep_task: Optional[asyncio.Task] = None
        self._path_resolution_task: Optional[asyncio.Task] = None

        # v3.6.2 §3.2: Adaptive LXMF identity announce timing.
        self._lxmf_announce_interval_steady = 2700  # 45 min
        self._lxmf_announce_interval_join = 300     # 5 min
        self._lxmf_announce_join_window = 900       # 15 min
        self._lxmf_announce_interval_min = 120      # 2 min
        # Startup discovery burst: several extra announces in the first
        # ~30 seconds with jitter so simultaneously restarted nodes can
        # find each other faster without permanently increasing network load.
        self._lxmf_startup_burst_delays = (4.0, 12.0, 24.0)
        self._lxmf_startup_burst_jitter = 2.0
        self._started_at: float = 0.0

        # Running state.
        self._running = False

        # Reference to the main asyncio event loop (set in start()).
        # Used by RNS thread callbacks to schedule async work via
        # call_soon_threadsafe. None until start() is called.
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Wakeup event for the path resolution loop.
        # Set by sync_board_catchup() to kick the loop immediately
        # instead of waiting for the 5-second poll interval.
        self._path_resolution_wakeup: Optional[asyncio.Event] = None

        # Throttle explicit immediate LXMF identity re-announces that are
        # triggered by membership changes or startup recovery.
        self._last_identity_announce_at: float = 0.0
        self._identity_reannounce_min_interval: float = 15.0

        # Catch-up dedupe to avoid repeated HAVE_REQ bursts when multiple
        # announces for the same peer/board arrive in a short window.
        self._catchup_schedule_cooldown: float = 2.0
        self._catchup_send_cooldown: float = 12.0
        self._recent_catchup_schedules: dict[str, float] = {}
        self._recent_catchup_sends: dict[tuple[str, str], float] = {}

    async def start(self) -> None:
        """
        Initialize the sync engine.

        1. Create LXMF router and register delivery callbacks.
        2. Register RNS request handlers for delta/payload serving.
        3. Start background tasks (HAVE loop, delta processor).
        """
        self._running = True
        self._started_at = time.time()

        # Set global engine reference for HAVE handler.
        set_sync_engine(self)

        # Load persisted peers from disk (§5.1).
        self.peer_tracker.load(PATH_PEERS_JSON)

        # Initialize LXMF if available.
        if HAS_LXMF:
            self._init_lxmf()
        else:
            RNS.log(
                "LXMF not installed — Tier 1 broadcast disabled. "
                "Install with: pip install lxmf",
                RNS.LOG_WARNING,
            )

        # Initialize dedicated payload transfer destination (§15).
        # This MUST be separate from lxmf.delivery because LXMF
        # intercepts all incoming Resources on its delivery destination
        # and tries to unpack them as LXMF messages, corrupting raw
        # payload data.  The retiboard.payload destination gives us
        # full control over incoming Resource acceptance.
        from retiboard.sync.payload_fetch import init_payload_destination
        self._payload_destination = init_payload_destination(self._identity)

        # Start background tasks.
        self._loop = asyncio.get_running_loop()
        self._path_resolution_wakeup = asyncio.Event()
        self._have_task = asyncio.create_task(self._have_loop())
        self._delta_task = asyncio.create_task(self._delta_processor())
        self._lxmf_announce_task = asyncio.create_task(self._lxmf_announce_loop())
        self._lxmf_startup_burst_task = asyncio.create_task(self._lxmf_startup_burst_loop())
        self._queue_sweep_task = asyncio.create_task(self._queue_eviction_loop())
        self._path_resolution_task = asyncio.create_task(self._path_resolution_loop())

        # Startup sync checkpoint: immediately trigger catch-up for all
        # subscribed boards using persisted peer data. This bypasses
        # the initial 5-15 min wait for the first HAVE broadcast cycle.
        if BOARDS_DIR.exists():
            for entry in BOARDS_DIR.iterdir():
                if entry.is_dir() and (entry / "meta.db").exists():
                    self.schedule_catchup(entry.name)

        RNS.log("Gossip sync engine started", RNS.LOG_INFO)

    async def stop(self) -> None:
        """Stop the sync engine and cancel background tasks."""
        self._running = False

        # Persist peers to disk (§5.1).
        self.peer_tracker.persist(PATH_PEERS_JSON)

        for task in [
            self._have_task,
            self._delta_task,
            self._lxmf_announce_task,
            self._lxmf_startup_burst_task,
            self._queue_sweep_task,
            self._path_resolution_task,
        ]:
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        RNS.log("Gossip sync engine stopped", RNS.LOG_INFO)

    def _init_lxmf(self) -> None:
        """
        Initialize the LXMF router and register handlers.

        LXMF API (verified against github.com/markqvist/LXMF examples):
            - LXMF.LXMRouter(storagepath=path)
            - router.register_delivery_identity(identity, display_name=name)
            - router.register_delivery_callback(callback)
        """
        lxmf_storage = str(RETIBOARD_HOME / "lxmf_storage")

        self._lxm_router = LXMF.LXMRouter(storagepath=lxmf_storage)

        # Register our delivery identity.
        self._lxmf_destination = self._lxm_router.register_delivery_identity(
            self._identity,
            display_name="RetiBoard Node",
        )

        # Tell the peer tracker which LXMF hash is ours so remote peer
        # candidate lists never include self.
        self.peer_tracker.set_self_hash(self._lxmf_destination.hexhash)

        # Register delivery callback for incoming messages (Tier 1 + Tier 3).
        from retiboard.sync.receiver import make_delivery_callback
        callback = make_delivery_callback(self.peer_tracker, sync_engine=self)
        self._lxm_router.register_delivery_callback(callback)

        # Register the Tier 3 delta handler on our delivery destination.
        # Payload transfer uses LXMF control plus dedicated payload resources;
        # there is no legacy direct payload request handler anymore.
        try:
            self._lxmf_destination.register_request_handler(
                PATH_DELTA,
                delta_request_handler,
                allow=RNS.Destination.ALLOW_ALL,
            )
            RNS.log(
                "Registered request handler for delta serving",
                RNS.LOG_DEBUG,
            )
        except Exception as e:
            RNS.log(
                f"Could not register request handlers on LXMF destination: {e}. "
                "Tier 3 serving may be unavailable.",
                RNS.LOG_WARNING,
            )

        RNS.log(
            f"LXMF router initialized. Delivery address: "
            f"{RNS.prettyhexrep(self._lxmf_destination.hash)}",
            RNS.LOG_INFO,
        )

        # v3.6.2 §3.2: Announce LXMF identity immediately so peers
        # can discover a path to our delivery destination.
        self._announce_lxmf_identity()

    def _announce_lxmf_identity(self) -> None:
        """
        Announce our LXMF delivery destination on the RNS network.

        v3.6.2 §3.2: Each node MUST periodically announce its LXMF identity.
        The app_data payload includes: {app, version}.
        We no longer include the full list of boards in the announce to avoid MTU limits.
        Instead, peers will request our board list via P2P LXMF upon receiving this announce.

        This makes our LXMF destination discoverable so other nodes can
        find a path to deliver LXMF messages (DELTA, PAYLOAD, metadata).
        Without this announce, peers know our peer_lxmf_hash from the
        board announce but RNS has no path to route messages to us.
        """
        if self._lxmf_destination is None:
            return
        try:
            app_data = json.dumps({
                "app": "retiboard",
                "version": "3.6.4",
            }, separators=(",", ":")).encode("utf-8")
            self._lxmf_destination.announce(app_data=app_data)
            self._last_identity_announce_at = time.time()
            RNS.log(
                f"Announced LXMF identity: "
                f"{RNS.prettyhexrep(self._lxmf_destination.hash)}",
                RNS.LOG_INFO,
            )
        except Exception as e:
            RNS.log(f"LXMF identity announce failed: {e}", RNS.LOG_WARNING)

    async def _lxmf_startup_burst_loop(self) -> None:
        """
        Send a bounded startup burst of extra LXMF identity announces.

        This improves simultaneous-restart discovery without changing the
        steady-state announce cadence. The burst is best-effort only and
        remains advisory like all announce-driven discovery.
        """
        if self._lxmf_destination is None:
            return

        last_offset = 0.0
        for base_delay in self._lxmf_startup_burst_delays:
            try:
                jitter = random.uniform(0.0, self._lxmf_startup_burst_jitter)
                target_offset = base_delay + jitter
                await asyncio.sleep(max(0.0, target_offset - last_offset))
                last_offset = target_offset
                if not self._running:
                    return
                self._announce_lxmf_identity()
            except asyncio.CancelledError:
                return
            except Exception as e:
                RNS.log(f"LXMF startup burst error: {e}", RNS.LOG_WARNING)
                await asyncio.sleep(1.0)

    async def _lxmf_announce_loop(self) -> None:
        """
        Periodic LXMF identity re-announce loop (v3.6.2 §3.2).

        Startup discovery is handled separately by a bounded burst loop.
        This loop keeps the normal accelerated join-window cadence and then
        settles into the steady-state interval. Jitter prevents announce storms.
        """
        RNS.log("LXMF identity announce loop started", RNS.LOG_INFO)

        while self._running:
            try:
                elapsed = time.time() - self._started_at
                if elapsed < self._lxmf_announce_join_window:
                    interval = self._lxmf_announce_interval_join
                else:
                    interval = self._lxmf_announce_interval_steady

                # Jitter ±10% (§6.3: jitter is mandatory).
                jitter = random.uniform(-interval * 0.1, interval * 0.1)
                wait = max(interval + jitter, self._lxmf_announce_interval_min)
                await asyncio.sleep(wait)

                self._announce_lxmf_identity()

            except asyncio.CancelledError:
                break
            except Exception as e:
                RNS.log(f"LXMF announce loop error: {e}", RNS.LOG_WARNING)
                await asyncio.sleep(60)

    # =========================================================================
    # Public API — called by posts route and other modules
    # =========================================================================

    def get_lxmf_hash(self) -> str:
        """Return this node's LXMF delivery hash (v3.6.2 §3.1)."""
        if self._lxmf_destination:
            return self._lxmf_destination.hexhash
        return ""

    def request_identity_reannounce(self, force: bool = False) -> None:
        """Trigger an immediate LXMF identity announce when membership changes."""
        if self._lxmf_destination is None:
            return
        now = time.time()
        if not force and (now - self._last_identity_announce_at) < self._identity_reannounce_min_interval:
            return
        self._announce_lxmf_identity()

    def send_lxmf(
        self,
        peer_lxmf_hash: str,
        payload: bytes,
        title: str,
        priority: Priority = Priority.DATA,
    ) -> SendResult:
        """
        Send an LXMF message to a peer, with queuing on path failure.

        Implements v3.6.2 §7.1 send logic:
          - path KNOWN → attempt immediate delivery, refresh TTL on success,
            mark STALE on failure, queue + request path.
          - path STALE → queue + request path, transition to REQUESTED.
          - path UNKNOWN/REQUESTED → queue, request path if UNKNOWN.

        §11.3: Messages for peers that exceeded max_path_retries MUST
        be rejected immediately rather than queued.

        Args:
            peer_lxmf_hash: Target peer's LXMF delivery hash.
            payload: UTF-8 bytes of message content.
            title: LXMF message type tag (e.g., MSG_TYPE_METADATA).
            priority: CONTROL or DATA priority tier.

        Returns:
            SendResult.SENT if handed to LXMF router for immediate delivery.
            SendResult.QUEUED if queued for later delivery (path not available).
            SendResult.REJECTED if dropped (peer unknown, unreachable, no router).
        """
        if not self._lxm_router or not self._lxmf_destination:
            return SendResult.REJECTED

        peer = self.peer_tracker.get_peer(peer_lxmf_hash)
        if peer is None:
            RNS.log(
                f"send_lxmf: unknown peer {peer_lxmf_hash[:16]}, dropping",
                RNS.LOG_DEBUG,
            )
            return SendResult.REJECTED

        # §11.3: reject immediately for unreachable peers past max retries.
        if peer.path_state == PathState.UNREACHABLE:
            if time.time() < peer.next_retry_at:
                RNS.log(
                    f"send_lxmf: peer {peer_lxmf_hash[:16]} unreachable, "
                    f"rejecting (§11.3)",
                    RNS.LOG_DEBUG,
                )
                return SendResult.REJECTED

        state = peer.path_state

        if state == PathState.KNOWN:
            # Attempt immediate delivery.
            sent = self._try_send_lxmf(peer, payload, title)
            if sent:
                self.peer_tracker.mark_path_known(peer_lxmf_hash)
                return SendResult.SENT
            else:
                # Mark stale, queue, request path (§7.1).
                self.peer_tracker.record_delivery_failure(peer_lxmf_hash)
                self.message_queue.queue_message(
                    peer_lxmf_hash, payload, title, priority,
                )
                self._request_path(peer_lxmf_hash)
                return SendResult.QUEUED

        elif state == PathState.STALE:
            # Queue and request path (§7.1).
            self.message_queue.queue_message(
                peer_lxmf_hash, payload, title, priority,
            )
            self._request_path(peer_lxmf_hash)
            return SendResult.QUEUED

        else:
            # UNKNOWN or REQUESTED: queue, request path if UNKNOWN.
            self.message_queue.queue_message(
                peer_lxmf_hash, payload, title, priority,
            )
            if state == PathState.UNKNOWN:
                self._request_path(peer_lxmf_hash)
            return SendResult.QUEUED

    def _try_send_lxmf(self, peer, payload: bytes, title: str) -> bool:
        """
        Attempt immediate LXMF delivery to a peer.

        Returns True on successful handoff to the LXMF router.
        """
        if peer.identity is None:
            return False
        try:
            dest = RNS.Destination(
                peer.identity,
                RNS.Destination.OUT,
                RNS.Destination.SINGLE,
                "lxmf", "delivery",
            )

            content_str = payload.decode("utf-8") if isinstance(payload, bytes) else payload
            method = LXMF.LXMessage.OPPORTUNISTIC
            if len(content_str) > 300:
                method = LXMF.LXMessage.DIRECT

            lxm = LXMF.LXMessage(
                dest,
                self._lxmf_destination,
                content_str,
                title,
                desired_method=method,
            )

            self._lxm_router.handle_outbound(lxm)

            RNS.log(
                f"LXMF sent to {peer.lxmf_hash[:16]} ({title})",
                RNS.LOG_DEBUG,
            )
            return True
        except Exception as e:
            RNS.log(
                f"LXMF send failed to {peer.lxmf_hash[:16]}: {e}",
                RNS.LOG_DEBUG,
            )
            return False

    def _request_path(self, peer_lxmf_hash: str) -> None:
        """
        Request an RNS path to a peer's LXMF destination (v3.6.2 §6).

        Transitions the peer to REQUESTED state and asks RNS Transport
        to discover a route.
        """
        peer = self.peer_tracker.get_peer(peer_lxmf_hash)
        if peer is None:
            return

        now = time.time()
        with self.peer_tracker._lock:
            # v3.6.4: If the path is already known or a request is in-flight
            # and hasn't timed out yet, skip redundant RNS traffic.
            if peer.path_state == PathState.KNOWN:
                # Path is already good.
                pass
            elif peer.path_state == PathState.REQUESTED:
                # Request already in flight. Check backoff (§14.4).
                if now < peer.next_retry_at:
                    return
            
            if peer.path_state != PathState.KNOWN:
                peer.path_state = PathState.REQUESTED
                # Set a baseline retry interval for path resolution (30s)
                # plus exponential backoff if it keeps failing.
                peer.next_retry_at = now + peer.next_retry_delay()

        try:
            dest_hash = bytes.fromhex(peer_lxmf_hash)
            if RNS.Transport.has_path(dest_hash):
                self.on_path_discovered(peer_lxmf_hash)
                return
            
            # v3.6.4: RNS.Transport.request_path is advisory; it sends a
            # discovery packet. We've already verified we don't have a path
            # and that we aren't spamming too fast.
            RNS.Transport.request_path(dest_hash)
            RNS.log(
                f"Path requested for peer {peer_lxmf_hash[:16]} "
                f"(retry {peer.retry_count})",
                RNS.LOG_DEBUG,
            )
        except Exception as e:
            RNS.log(
                f"Path request failed for {peer_lxmf_hash[:16]}: {e}",
                RNS.LOG_DEBUG,
            )

    def on_path_discovered(self, peer_lxmf_hash: str) -> None:
        """
        Called when RNS discovers a path to a peer (v3.6.2 §7.2).

        Sets path state to KNOWN, resets retry count, and flushes
        the queued messages in priority order (§8.2).
        """
        self.peer_tracker.record_delivery_success(peer_lxmf_hash)

        # Signal pending chunk sessions to retry immediately (§15.2).
        from retiboard.sync.payload_fetch import signal_path_discovered
        signal_path_discovered(peer_lxmf_hash)

        # v3.6.3: Wake up the sync evaluation loops to immediately re-check
        # all boards for this peer now that we can talk to them.
        if self._path_resolution_wakeup:
            self._path_resolution_wakeup.set()

        # §8.2: Flush queue in priority order.
        messages = self.message_queue.flush_peer(peer_lxmf_hash)
        if not messages:
            return

        RNS.log(
            f"Path discovered for {peer_lxmf_hash[:16]}, "
            f"flushing {len(messages)} queued message(s)",
            RNS.LOG_INFO,
        )

        peer = self.peer_tracker.get_peer(peer_lxmf_hash)
        sent = 0
        failed = []
        for msg in messages:
            if msg.expired:
                continue
            if peer and self._try_send_lxmf(peer, msg.payload, msg.title):
                sent += 1
            else:
                # §8.2: delivery fails mid-flush → remaining stay queued.
                failed.append(msg)
                # Also queue everything we haven't tried yet.
                idx = messages.index(msg)
                failed.extend(m for m in messages[idx + 1:] if not m.expired)
                break

        if failed:
            self.message_queue.requeue_failed(peer_lxmf_hash, failed)
            self.peer_tracker.record_delivery_failure(peer_lxmf_hash)

        if sent > 0:
            RNS.log(
                f"Flushed {sent} message(s) to {peer_lxmf_hash[:16]}, "
                f"{len(failed)} re-queued",
                RNS.LOG_DEBUG,
            )

    async def _queue_eviction_loop(self) -> None:
        """
        Periodic TTL eviction sweep for message queues (v3.6.2 §8.1).

        Runs every QUEUE_EVICTION_SWEEP_INTERVAL seconds.
        """
        RNS.log("Queue eviction sweep loop started", RNS.LOG_INFO)
        while self._running:
            try:
                await asyncio.sleep(QUEUE_EVICTION_SWEEP_INTERVAL)
                self.message_queue.sweep_expired()
                self.peer_tracker.sweep_expired()
                self.peer_tracker.persist(PATH_PEERS_JSON)
                self._prune_recent_catchup_tracking()
            except asyncio.CancelledError:
                break
            except Exception as e:
                RNS.log(f"Queue sweep error: {e}", RNS.LOG_WARNING)

    async def _path_resolution_loop(self) -> None:
        """
        Periodic path resolution checker (v3.6.2 §6, §7.2).

        RNS does not provide a push callback for path discovery. We
        poll RNS.Transport.has_path() for peers in REQUESTED state
        and trigger on_path_discovered() when a path appears.

        Also re-requests paths for STALE peers that have queued messages.

        Supports immediate wakeup via _path_resolution_wakeup event,
        triggered by sync_board_catchup() when a new board subscription
        or peer discovery needs fast path resolution.

        Runs every 5 seconds normally, immediately on wakeup signal.
        """
        RNS.log("Path resolution loop started", RNS.LOG_DEBUG)
        while self._running:
            try:
                # Wait for either the 5-second timeout or a wakeup signal.
                if self._path_resolution_wakeup:
                    self._path_resolution_wakeup.clear()
                    try:
                        await asyncio.wait_for(
                            self._path_resolution_wakeup.wait(),
                            timeout=1.0,
                        )
                    except asyncio.TimeoutError:
                        pass
                else:
                    await asyncio.sleep(1.0)

                # Check all peers with queued messages or in REQUESTED state.
                with self.peer_tracker._lock:
                    candidates = [
                        (h, p) for h, p in self.peer_tracker._peers.items()
                        if p.path_state in (PathState.REQUESTED, PathState.STALE)
                        or self.message_queue.has_queued(h)
                    ]

                for peer_hash, peer in candidates:
                    try:
                        dest_bytes = bytes.fromhex(peer_hash)
                        if RNS.Transport.has_path(dest_bytes):
                            if peer.path_state != PathState.KNOWN:
                                RNS.log(
                                    f"Path resolved for {peer_hash[:16]}",
                                    RNS.LOG_DEBUG,
                                )
                                self.on_path_discovered(peer_hash)
                    except Exception:
                        pass

            except asyncio.CancelledError:
                break
            except Exception as e:
                RNS.log(f"Path resolution loop error: {e}", RNS.LOG_DEBUG)

    def _prune_recent_catchup_tracking(self) -> None:
        cutoff = time.time() - (2 * max(
            self._catchup_schedule_cooldown,
            self._catchup_send_cooldown,
            1.0,
        ))
        self._recent_catchup_schedules = {
            board_id: ts
            for board_id, ts in self._recent_catchup_schedules.items()
            if ts >= cutoff
        }
        self._recent_catchup_sends = {
            key: ts
            for key, ts in self._recent_catchup_sends.items()
            if ts >= cutoff
        }

    def on_local_post_created(self, post, board_id: str) -> None:
        """
        Called when a new post is created locally (via API).

        Triggers Tier 1 broadcast and opportunistic replication (§7.1).
        """
        if self._lxm_router and self._lxmf_destination:
            from retiboard.sync.broadcast import broadcast_metadata
            from retiboard.sync.replication import replicate_metadata
            
            # 1. Full broadcast (§7.1 Tier 1)
            broadcast_metadata(
                self._lxm_router,
                self._lxmf_destination,
                post,
                board_id,
                self.peer_tracker,
                sync_engine=self,
            )

            # 2. Opportunistic push (§7.1)
            asyncio.create_task(replicate_metadata(
                self._lxm_router,
                self._lxmf_destination,
                post,
                board_id,
                self.peer_tracker,
                sync_engine=self,
            ))

    def enqueue_delta_request(
        self,
        board_id: str,
        thread_id: str,
        since_timestamp: int,
        known_post_count: int,
        target_hash: Optional[bytes] = None,
        target_identity: Optional[RNS.Identity] = None,
    ) -> None:
        """
        Enqueue a DELTA_REQUEST for background processing.

        Called by the HAVE handler when stale threads are detected.
        """
        req = DeltaRequest(
            board_id=board_id,
            thread_id=thread_id,
            since_timestamp=since_timestamp,
            known_post_count=known_post_count,
            target_hash=target_hash,
            target_identity=target_identity,
        )
        try:
            self._delta_queue.put_nowait(req)
        except asyncio.QueueFull:
            RNS.log("Delta request queue full, dropping oldest", RNS.LOG_DEBUG)

    async def fetch_payload(
        self,
        board_id: str,
        content_hash: str,
        expected_size: int | None = None,
        manual_override: bool = False,
    ) -> bool:
        """
        Fetch a payload from peers (on-demand) via LXMF message exchange.

        Called by the posts API when a payload is requested but missing locally.
        Sends PAYLOAD_REQUEST LXMF messages to peers and waits for a
        PAYLOAD_RESPONSE to arrive (v3.6.2 §7.1 Tier 3).

        Passes sync_engine=self so the fetcher can use send_lxmf() for
        the request, and self_lxmf_hash to exclude ourselves from the
        peer list.
        """
        from retiboard.sync.payload_fetch import fetch_payload_from_peers
        return await fetch_payload_from_peers(
            board_id,
            content_hash,
            self.peer_tracker,
            self_lxmf_hash=self.get_lxmf_hash(),
            sync_engine=self,
            expected_size=expected_size,
            manual_override=manual_override,
        )

    def send_board_announces_to_peer(self, peer_lxmf_hash: str) -> int:
        """
        Push board announce data for all locally owned boards to a peer.

        Called when a new peer is discovered via LXMF identity announce
        (§8.2) to solve the cold-start race: if the peer wasn't reachable
        when we originally broadcast a board announce, they never learned
        about our boards. By re-sending announces via LXMF on peer
        discovery, late-joining peers can discover boards they missed.

        This sends the full §3.3 announce payload (including key_material)
        as an LXMF message with MSG_TYPE_BOARD_ANNOUNCE. The receiver
        processes it through the same path as an RNS broadcast announce,
        adding the board to their discovered-boards list.

        Only sends announces for boards we OWN (have an RNS Destination
        for). Subscribed-but-not-owned boards are not re-announced — their
        original creator is responsible for that.

        Respects the vision:
          - key_material is public per §5 ("anyone who obtains the announce
            can derive the board key") — sending it via LXMF is equivalent
            to the original RNS broadcast.
          - The peer decides whether to subscribe — no auto-subscribe.
          - Capped at 10 boards to limit burst traffic on peer discovery.

        Args:
            peer_lxmf_hash: The new peer's LXMF delivery hash.

        Returns:
            Number of board announces sent.
        """
        if not self._lxm_router or not self._lxmf_destination:
            return 0

        from retiboard.sync import MSG_TYPE_BOARD_ANNOUNCE

        # Don't send to ourselves.
        self_hash = self._lxmf_destination.hexhash
        if peer_lxmf_hash == self_hash:
            return 0

        owned = self._board_manager._owned_destinations
        if not owned:
            return 0

        sent = 0
        for board_id, destination in list(owned.items())[:10]:
            # Get the full Board object with key_material.
            # We need key_material for the announce — it's what lets the
            # receiver derive the board key (§5).
            km = self._board_manager.get_key_material(board_id)
            if not km:
                RNS.log(
                    f"Skipping board announce push for {board_id[:8]}: "
                    f"no key_material in cache",
                    RNS.LOG_DEBUG,
                )
                continue

            # Build the announce dict directly from cached data.
            # We can't use build_announce_data(board) because that
            # requires a Board object with key_material populated,
            # and the DB-stored Board has key_material stripped.
            # Instead, load announce from the on-disk cache.
            from retiboard.boards.subscribe import load_announce_cache
            announce_dict = load_announce_cache(board_id)
            if announce_dict is None:
                RNS.log(
                    f"Skipping board announce push for {board_id[:8]}: "
                    f"no announce cache on disk",
                    RNS.LOG_DEBUG,
                )
                continue

            # Ensure peer_lxmf_hash is current (it may have been empty
            # at board creation if the LXMF destination wasn't ready).
            if "plh" in announce_dict:
                announce_dict["plh"] = self_hash
            elif "peer_lxmf_hash" in announce_dict:
                announce_dict["peer_lxmf_hash"] = self_hash

            payload = json.dumps(
                announce_dict,
                separators=(",", ":"),
            ).encode("utf-8")

            ok = self.send_lxmf(
                peer_lxmf_hash,
                payload,
                MSG_TYPE_BOARD_ANNOUNCE,
                Priority.CONTROL,
            )
            if ok:
                sent += 1
                self._recent_catchup_sends[(board_id, peer_lxmf_hash)] = time.time()

        if sent > 0:
            RNS.log(
                f"Pushed {sent} board announce(s) to new peer "
                f"{peer_lxmf_hash[:16]}",
                RNS.LOG_INFO,
            )

        return sent

    async def sync_board_catchup(self, board_id: str) -> None:
        """
        Trigger immediate catch-up sync for a board.

        Called on:
          1. New board subscription (user joins a board)
          2. New peer discovery for a board we participate in

        Sends a lightweight HAVE_REQ to known peers for this board,
        requesting they immediately respond with their current HAVE.
        This accelerates recovery from missed HAVE broadcasts without
        waiting for the next periodic cycle (5-15 min, or 30-60 min LoRa).

        Respects the vision:
          - Only requests active threads (§7.1: abandoned excluded)
          - No content exchanged (pure structural metadata)
          - User-sovereign: only triggers for boards we're subscribed to
          - Fan-out capped at 5 peers to limit network load

        Spec basis: §7.1 Tier 2 + §13.1 (LXMF direct preferred for known peers).
        """
        if not self._lxm_router or not self._lxmf_destination:
            return

        self_hash = self._lxmf_destination.hexhash
        peers = self.peer_tracker.get_fetch_peers(board_id, count=5)
        peers = [p for p in peers if p.lxmf_hash != self_hash]

        now = time.time()
        recent_catchup_sends = getattr(self, "_recent_catchup_sends", None)
        if not isinstance(recent_catchup_sends, dict):
            recent_catchup_sends = {}
            self._recent_catchup_sends = recent_catchup_sends

        send_cooldown = getattr(self, "_catchup_send_cooldown", 12.0)
        if not isinstance(send_cooldown, (int, float)):
            send_cooldown = 12.0

        filtered_peers = []
        for peer in peers:
            last_sent = recent_catchup_sends.get((board_id, peer.lxmf_hash), 0.0)
            if now - last_sent < send_cooldown:
                RNS.log(
                    f"Catch-up: suppressing duplicate HAVE_REQ for board {board_id[:8]} "
                    f"to {peer.lxmf_hash[:16]} ({now - last_sent:.1f}s since last)",
                    RNS.LOG_DEBUG,
                )
                continue
            filtered_peers.append(peer)
        peers = filtered_peers

        if not peers:
            RNS.log(
                f"Catch-up: no peers for board {board_id[:8]}, "
                f"will sync on next HAVE cycle",
                RNS.LOG_DEBUG,
            )
            return

        # Send lightweight HAVE_REQ: just the board_id. The recipient
        # responds with their full HAVE for that board.
        from retiboard.sync import MSG_TYPE_HAVE_REQ
        request = json.dumps({"board_id": board_id}, separators=(",", ":")).encode("utf-8")

        sent_immediate = 0
        sent_queued = 0
        for peer in peers[:5]:  # cap fan-out
            if peer.identity is None:
                continue
            result = self.send_lxmf(
                peer.lxmf_hash,
                request,
                MSG_TYPE_HAVE_REQ,
                Priority.CONTROL,
            )
            if result == SendResult.SENT:
                sent_immediate += 1
            elif result == SendResult.QUEUED:
                sent_queued += 1

        total = sent_immediate + sent_queued
        RNS.log(
            f"Catch-up: HAVE_REQ for board {board_id[:8]} "
            f"to {total}/{len(peers)} peer(s) "
            f"({sent_immediate} sent, {sent_queued} queued)",
            RNS.LOG_INFO,
        )

        # Kick the path resolution loop immediately so queued HAVE_REQ
        # messages are delivered as fast as possible (instead of waiting
        # up to 5 seconds for the next poll cycle).
        if self._path_resolution_wakeup:
            self._path_resolution_wakeup.set()

    def schedule_catchup(self, board_id: str) -> None:
        """
        Thread-safe wrapper to schedule sync_board_catchup on the main loop.

        Called from RNS transport thread callbacks (announce handlers, LXMF
        delivery callbacks) which run outside the asyncio event loop.
        asyncio.get_running_loop() would raise RuntimeError in those threads.

        Uses loop.call_soon_threadsafe to safely schedule the coroutine
        on the engine's event loop.
        """
        if self._loop is None or self._loop.is_closed():
            RNS.log(
                f"schedule_catchup: event loop not available for board {board_id[:8]}",
                RNS.LOG_DEBUG,
            )
            return

        now = time.time()
        last = self._recent_catchup_schedules.get(board_id, 0.0)
        if now - last < self._catchup_schedule_cooldown:
            RNS.log(
                f"schedule_catchup: suppressed duplicate scheduling for board {board_id[:8]} "
                f"({now - last:.1f}s since last)",
                RNS.LOG_DEBUG,
            )
            return
        self._recent_catchup_schedules[board_id] = now

        def _schedule():
            self._loop.create_task(self.sync_board_catchup(board_id))

        try:
            self._loop.call_soon_threadsafe(_schedule)
        except RuntimeError:
            # Loop is closed or shutting down.
            RNS.log(
                f"schedule_catchup: loop closed for board {board_id[:8]}",
                RNS.LOG_DEBUG,
            )

    def get_active_sync_tasks(self) -> dict:
        """Return lists of active background sync tasks (§7)."""
        now = time.time()
        # A catch-up is considered "active" if it was scheduled or sent
        # in the last 120 seconds (v3.6.3: increased to cover RNS path-resolve latency).
        active_catchups = set()
        for board_id, ts in self._recent_catchup_schedules.items():
            if now - ts < 120.0:
                active_catchups.add(board_id)
        
        for (board_id, peer), ts in self._recent_catchup_sends.items():
            if now - ts < 120.0:
                active_catchups.add(board_id)

        return {
            "catchup_boards": list(active_catchups),
            "delta_queue_size": self._delta_queue.qsize(),
        }

    # =========================================================================
    # Background loops
    # =========================================================================

    async def _have_loop(self) -> None:
        """
        Periodic HAVE broadcast loop (Tier 2).

        Broadcasts an initial HAVE immediately after startup (with a brief
        delay for transport initialization), then continues at adaptive
        intervals (§7.1: 5-15 min normal, 30-60 min LoRa).

        Transport awareness (§14.4): re-checks is_low_bandwidth() each
        cycle so the loop adapts if interfaces change at runtime (e.g.,
        a LoRa interface comes online after boot).
        """
        RNS.log("HAVE broadcast loop started", RNS.LOG_INFO)

        # Brief initial delay: let LXMF announce propagate and peers
        # discover us before we broadcast HAVEs.  Without this, HAVEs
        # go out before any peer knows our LXMF hash, making delta
        # requests impossible.
        await asyncio.sleep(8)
        first_run = True

        while self._running:
            try:
                if not first_run:
                    # Re-check bandwidth each cycle for dynamic adaptation.
                    low_bw = is_low_bandwidth()
                    min_interval, max_interval = get_have_interval(low_bw)

                    # Adaptive interval with jitter.
                    interval = random.randint(min_interval, max_interval)
                    await asyncio.sleep(interval)
                else:
                    first_run = False

                low_bw = is_low_bandwidth()

                if not BOARDS_DIR.exists():
                    continue

                # Broadcast HAVE for each subscribed board.
                for entry in BOARDS_DIR.iterdir():
                    if not entry.is_dir() or not (entry / "meta.db").exists():
                        continue

                    board_id = entry.name
                    try:
                        have = await build_have_packet(
                            board_id,
                            is_low_bandwidth=low_bw,
                            peer_tracker=self.peer_tracker,
                        )
                        if have is None:
                            continue

                        have_bytes = serialize_have(have)

                        # Check size constraint (§7.1: target <1 KB).
                        if len(have_bytes) > 1024:
                            RNS.log(
                                f"HAVE for board {board_id[:8]} exceeds 1 KB "
                                f"({len(have_bytes)} bytes), truncating",
                                RNS.LOG_DEBUG,
                            )

                        RNS.log(
                            f"HAVE broadcast: board {board_id[:8]}, "
                            f"{len(have.get('active_threads', []))} thread(s), "
                            f"{len(have_bytes)} bytes",
                            RNS.LOG_DEBUG,
                        )

                        # Broadcast HAVE via two complementary paths:
                        #
                        # 1. Board announce (if we own it): reaches nodes
                        #    that haven't discovered us via LXMF yet.
                        #    BUT: the receiver gets the board dest hash, not our
                        #    LXMF hash, and identity resolution often fails
                        #    (board dest identity != LXMF delivery identity).
                        #
                        # 2. LXMF to known peers: reliable, the source_hash IS
                        #    our LXMF hash (authoritative per §9.2), so the
                        #    receiver can always resolve us for delta requests.
                        #
                        # Both paths are used for owned boards. Non-owned boards
                        # only use LXMF (no board destination to announce from).
                        owned_dest = self._board_manager._owned_destinations.get(board_id)
                        if owned_dest:
                            # v3.6.3: MTU Hardening (§15).
                            # RNS Announces are NOT fragmented. If app_data exceeds
                            # MTU (~384 bytes), it raises OSError.
                            from retiboard.config import MAX_ANNOUNCE_APP_DATA
                            if len(have_bytes) <= MAX_ANNOUNCE_APP_DATA:
                                try:
                                    owned_dest.announce(app_data=have_bytes)
                                except IOError as e:
                                    RNS.log(
                                        f"HAVE announce for {board_id[:8]} failed: {e}",
                                        RNS.LOG_DEBUG,
                                    )
                            else:
                                RNS.log(
                                    f"HAVE for {board_id[:8]} too large for announce "
                                    f"({len(have_bytes)}B > {MAX_ANNOUNCE_APP_DATA}B), "
                                    "sending via LXMF only.",
                                    RNS.LOG_DEBUG,
                                )

                        # Always send HAVE via LXMF to known peers, regardless
                        # of whether we own the board. This is the reliable path
                        # that works with the dual-destination model (§2.3).
                        if self._lxm_router and self._lxmf_destination:
                            from retiboard.sync import MSG_TYPE_HAVE
                            self_hash = self._lxmf_destination.hexhash
                            peers = self.peer_tracker.get_direct_have_peers(
                                board_id,
                                exclude_hash=self_hash,
                                count=5,
                            )
                            for peer in peers:
                                if peer.identity is None:
                                    continue
                                self.send_lxmf(
                                    peer.lxmf_hash,
                                    have_bytes,
                                    MSG_TYPE_HAVE,
                                    Priority.CONTROL,
                                )
                                self.peer_tracker.mark_direct_have_sent(peer.lxmf_hash)

                    except Exception as e:
                        RNS.log(
                            f"HAVE broadcast error for board {board_id[:8]}: {e}",
                            RNS.LOG_DEBUG,
                        )

            except asyncio.CancelledError:
                break
            except Exception as e:
                RNS.log(f"HAVE loop error: {e}", RNS.LOG_WARNING)
                await asyncio.sleep(60)  # Back off on error

    async def _delta_processor(self) -> None:
        """
        Background processor for queued DELTA_REQUESTs.

        Dequeues requests and sends them as LXMF messages to the target
        peer.  Responses arrive asynchronously via the LXMF delivery
        callback (MSG_TYPE_DELTA_RES handler in receiver.py).

        Respects rate limiting (§7.1: max 5 concurrent syncs per board).
        """
        RNS.log("Delta request processor started", RNS.LOG_INFO)

        while self._running:
            try:
                # Wait for a delta request.
                req = await asyncio.wait_for(
                    self._delta_queue.get(), timeout=30.0,
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            try:
                # Rate limit check.
                if req.target_hash:
                    peer_hex = req.target_hash.hex()
                    if not self.rate_limiter.can_sync_peer(
                        req.board_id, peer_hex,
                    ):
                        RNS.log(
                            f"Delta request for {req.thread_id[:12]} in backoff",
                            RNS.LOG_DEBUG,
                        )
                        continue

                # Acquire sync slot (limits concurrent delta sends).
                await self.rate_limiter.acquire(req.board_id)
                try:
                    await self._execute_delta_request(req)
                finally:
                    self.rate_limiter.release(req.board_id)

            except Exception as e:
                RNS.log(
                    f"Delta processor error: {e}",
                    RNS.LOG_WARNING,
                )

    async def _execute_delta_request(self, req: DeltaRequest) -> None:
        """
        Execute a single DELTA_REQUEST by sending it as an LXMF message.

        =====================================================================
        DEBUGGING HISTORY — DO NOT USE RNS LINK REQUESTS FOR DELTA SYNC
        =====================================================================
        Previous implementations used RNS Link.request() to send delta
        requests and receive responses synchronously.  This NEVER worked
        reliably because:

        1. RNS request handlers run synchronously in the transport thread.
        2. build_delta_response() is async (SQLite via aiosqlite).
        3. Every attempt to bridge sync→async in the handler blocks the
           transport thread:
             - asyncio.run() → blocks thread, link times out
             - ThreadPoolExecutor + future.result() → same blocking
             - All variants produce: "Attempt to transmit over a closed
               link, dropping packet" on the responder, and the requester
               records a failure with exponential backoff.
        4. The LXMF router may also interfere with link lifecycle on the
           lxmf.delivery destination.

        FIX: Send DELTA_REQUEST as an LXMF message (MSG_TYPE_DELTA_REQ).
        The receiver builds the response asynchronously and sends it back
        as another LXMF message (MSG_TYPE_DELTA_RES).  This mirrors the
        HAVE_REQ/HAVE exchange which works flawlessly.

        Retry is handled naturally by the HAVE cycle: if threads are
        still stale at the next HAVE comparison, new delta requests are
        enqueued automatically.  No explicit timeout/failure tracking
        needed for individual requests.
        =====================================================================
        """
        if not self._lxm_router or not self._lxmf_destination:
            return

        target_hex = req.target_hash.hex() if req.target_hash else None
        if not target_hex:
            RNS.log(
                f"Cannot execute delta request: no target hash "
                f"for thread {req.thread_id[:12]}",
                RNS.LOG_DEBUG,
            )
            return

        RNS.log(
            f"Executing delta request: board {req.board_id[:8]}, "
            f"thread {req.thread_id[:12]}, since {req.since_timestamp}",
            RNS.LOG_DEBUG,
        )

        request_data = json.dumps({
            "board_id": req.board_id,
            "thread_id": req.thread_id,
            "since_timestamp": req.since_timestamp,
            "known_post_count": req.known_post_count,
        }, separators=(",", ":")).encode("utf-8")

        from retiboard.sync import MSG_TYPE_DELTA_REQ
        result = self.send_lxmf(
            target_hex,
            request_data,
            MSG_TYPE_DELTA_REQ,
            Priority.CONTROL,
        )

        if result == SendResult.SENT:
            RNS.log(
                f"DELTA_REQ sent to {target_hex[:16]} "
                f"for thread {req.thread_id[:12]}",
                RNS.LOG_DEBUG,
            )
        elif result == SendResult.QUEUED:
            RNS.log(
                f"DELTA_REQ queued for {target_hex[:16]} "
                f"for thread {req.thread_id[:12]} (awaiting path)",
                RNS.LOG_DEBUG,
            )
        else:
            RNS.log(
                f"DELTA_REQ rejected for {target_hex[:16]}",
                RNS.LOG_DEBUG,
            )
