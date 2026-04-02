"""
RetiBoard entry point.

Usage:
    python -m retiboard              # Full client (backend + Vue SPA)
    python -m retiboard --relay      # Headless relay (gossip + pruning only)

Spec references:
    §2.2  — User node architecture, relay mode definition
    §2.4  — "Relay-mode nodes apply exactly the same storage and pruning
            rules as regular clients [...] The only difference is the
            absence of the Vue SPA and local UI."
    §9    — Board discovery (announce handler registration)
    §15   — Deployment: single executable, default port 8787, --relay flag

Startup sequence:
    1. Parse args (--relay, --port, --verbose)
    2. Ensure ~/.retiboard/ data directories exist
    3. Initialize Reticulum transport
    4. Load or create persistent RNS identity (§18.1)
    5. Create BoardManager (registers announce handler with RNS Transport)
    6. Create SyncEngine (gossip + LXMF router)
    7. Recover subscribed boards' key_material from disk cache
    8a. [Client mode] Start FastAPI on 127.0.0.1:8787, serve Vue SPA
    8b. [Relay mode]  Run headless — gossip + pruning loop only

Relay mode design:
    No separate relay.py needed. The relay is simply the same node without
    FastAPI/Uvicorn/Vue. The SyncEngine and pruner are identical modules
    shared with client mode — no relay-specific storage or pruning paths
    exist (§2.4). Relay nodes are structurally content-blind: no key_material
    is ever loaded, no decryption code path exists in the backend.
"""

import argparse
import asyncio
import logging
import os
import secrets
import signal

import RNS

from retiboard.config import (
    APP_NAME,
    APP_VERSION,
    SPEC_VERSION,
    API_HOST,
    API_PORT,
    LOG_PATH,
    RETIBOARD_HOME,
    DEFAULT_RNS_CONFIG,
)
from retiboard.rns_identity import load_or_create_identity, ensure_data_dirs
from retiboard.api import create_app
from retiboard.boards.manager import BoardManager
from retiboard.logging_config import (
    bridge_rns_log,
    configure_logging,
    render_access_banner,
    resolve_log_file,
    LoggingRuntime,
)
from retiboard.sync.engine import SyncEngine


def parse_args(argv=None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="retiboard",
        description=(
            f"{APP_NAME} v{APP_VERSION} — "
            f"Sovereign imageboard over Reticulum (spec v{SPEC_VERSION})"
        ),
    )
    parser.add_argument(
        "--relay",
        action="store_true",
        help=(
            "Run in relay mode: headless, no UI. Participates in gossip "
            "and pruning identically to a full client (spec §2.4). "
            "Relay nodes are structurally content-blind."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=API_PORT,
        help=f"HTTP port for the local API/SPA (default: {API_PORT}). "
             "Ignored in relay mode.",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="count",
        default=0,
        help=(
            "Increase log verbosity. Default = INFO, "
            "-v = Reticulum verbose, -vv = DEBUG."
        ),
    )
    parser.add_argument(
        "--log-to-console",
        action="store_true",
        help=(
            "Mirror runtime logs to the console in addition to the "
            f"rotating log file at {LOG_PATH}."
        ),
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help=(
            "Path to the runtime log file. "
            f"Default: {LOG_PATH}"
        ),
    )
    return parser.parse_args(argv)


def _rns_log_level(verbosity: int) -> int:
    """Map CLI verbosity to RNS log level constants."""
    if verbosity >= 2:
        return RNS.LOG_DEBUG
    elif verbosity >= 1:
        return RNS.LOG_VERBOSE
    else:
        return RNS.LOG_INFO


def init_reticulum(verbosity: int) -> RNS.Reticulum:
    """
    Initialize the Reticulum transport layer.

    This creates or connects to the local Reticulum instance. If rnsd is
    running, Reticulum will use the shared instance. Otherwise it starts
    a standalone transport.
    """
    # Route Reticulum output into the shared Python logging pipeline
    # before the first startup message.
    RNS.logdest = RNS.LOG_CALLBACK
    RNS.logcall = bridge_rns_log
    RNS.logfile = None
    RNS.loglevel = _rns_log_level(verbosity)

    RNS.log(
        f"Initializing {APP_NAME} v{APP_VERSION} (spec v{SPEC_VERSION})",
        RNS.LOG_INFO,
    )

    rns_config_dir = os.path.expanduser("~/.reticulum")
    rns_config_path = os.path.join(rns_config_dir, "config")
    
    if not os.path.exists(rns_config_path):
        RNS.log("Creating default Reticulum configuration", RNS.LOG_INFO)
        os.makedirs(rns_config_dir, exist_ok=True)
        with open(rns_config_path, "w") as f:
            f.write(DEFAULT_RNS_CONFIG)

    # RNS.Reticulum() auto-detects whether a shared instance (rnsd) is
    # available. If not, it starts a standalone transport.
    reticulum = RNS.Reticulum(
        configdir=None,
        loglevel=_rns_log_level(verbosity),
        logdest=bridge_rns_log,
    )

    RNS.log("Reticulum transport initialized", RNS.LOG_INFO)
    return reticulum


def run_client(
    port: int,
    board_manager: BoardManager,
    sync_engine: SyncEngine,
    identity,
    logging_runtime: LoggingRuntime,
) -> None:
    """
    Start the full client: FastAPI backend + Vue SPA on localhost.
    """
    import uvicorn

    # Generate high-entropy ephemeral API token (§15).
    api_token = secrets.token_urlsafe(32)

    app = create_app(
        relay_mode=False,
        board_manager=board_manager,
        sync_engine=sync_engine,
        identity=identity,
        api_token=api_token,
    )

    # Output the secure tokenized URL (§15) to console only. The token must
    # never be persisted to the runtime log file.
    print()
    print(
        render_access_banner(
            host=API_HOST,
            port=port,
            token=api_token,
            log_file=logging_runtime.log_file,
            log_to_console=logging_runtime.log_to_console,
        )
    )
    print()

    logging.getLogger("retiboard.main").info(
        "Local HTTP listener ready at http://%s:%s?token=[redacted]",
        API_HOST,
        port,
    )

    # Uvicorn runs in the main thread. Gossip and pruning run as
    # background asyncio tasks via FastAPI lifespan events.
    uvicorn.run(
        app,
        host=API_HOST,
        port=port,
        log_level=logging_runtime.uvicorn_log_level,
        log_config=logging_runtime.uvicorn_log_config,
        access_log=True,
    )


def run_relay(board_manager: BoardManager, sync_engine: SyncEngine) -> None:
    """
    Start relay mode: headless, no UI, no HTTP server.
    """
    from retiboard.pruning.scheduler import _prune_loop

    RNS.log(
        f"{APP_NAME} relay mode starting. No HTTP server. "
        "Running pruning + gossip sync (same rules as client, §2.4).",
        RNS.LOG_INFO,
    )

    async def _relay_main() -> None:
        """
        Main relay coroutine.
        """
        shutdown_event = asyncio.Event()

        def _signal_handler() -> None:
            """Handle SIGINT/SIGTERM by setting the shutdown event."""
            RNS.log("Relay received shutdown signal", RNS.LOG_INFO)
            shutdown_event.set()

        # Register signal handlers on the running event loop.
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _signal_handler)
            except NotImplementedError:
                pass

        # Start the sync engine (initializes LXMF, registers handlers,
        # launches HAVE loop, delta processor, announce loop, etc.).
        await sync_engine.start()

        # Start the prune loop as a concurrent background task.
        prune_task = asyncio.create_task(_prune_loop())

        RNS.log(
            f"{APP_NAME} relay running. Gossip + pruning active. "
            "Press Ctrl+C to stop.",
            RNS.LOG_INFO,
        )

        try:
            # Block until shutdown signal.
            await shutdown_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            # Clean shutdown: stop sync engine, cancel prune task.
            RNS.log("Relay shutting down...", RNS.LOG_INFO)

            prune_task.cancel()
            try:
                await prune_task
            except asyncio.CancelledError:
                pass

            await sync_engine.stop()

            from retiboard.db.pool import get_pool
            await get_pool().close_all()

            RNS.log("Relay shutdown complete.", RNS.LOG_INFO)

    try:
        asyncio.run(_relay_main())
    except KeyboardInterrupt:
        RNS.log("Relay interrupted.", RNS.LOG_INFO)


def main() -> None:
    """Entry point for `python -m retiboard` and the `retiboard` console script."""
    args = parse_args()

    # 1. Ensure data directories exist
    ensure_data_dirs()

    # 2. Configure unified process logging before startup logs begin.
    logging_runtime = configure_logging(
        log_file=resolve_log_file(args.log_file),
        log_to_console=args.log_to_console,
        verbosity=args.verbose,
    )

    # 3. Initialize Reticulum transport
    init_reticulum(args.verbose)

    RNS.log(f"Data directory: {RETIBOARD_HOME}", RNS.LOG_INFO)
    RNS.log(f"Runtime log file: {logging_runtime.log_file}", RNS.LOG_INFO)

    # 4. Load or create persistent identity
    identity = load_or_create_identity()
    RNS.log(
        f"Node identity hash: {identity.hexhash}",
        RNS.LOG_INFO,
    )

    # 5. Create BoardManager (registers announce handler with RNS Transport)
    board_manager = BoardManager(identity)

    # 6. Create SyncEngine (gossip sync, §7)
    sync_engine = SyncEngine(identity, board_manager)

    # 7. Connect sync engine to board manager for peer tracking.
    board_manager.set_sync_engine(sync_engine)

    # 8. Recover subscribed boards' key_material from on-disk announce cache.
    asyncio.run(board_manager.recover_boards_on_startup())

    # 9. Launch in the appropriate mode
    if args.relay:
        run_relay(board_manager, sync_engine)
    else:
        run_client(
            port=args.port,
            board_manager=board_manager,
            sync_engine=sync_engine,
            identity=identity,
            logging_runtime=logging_runtime,
        )


if __name__ == "__main__":
    main()
