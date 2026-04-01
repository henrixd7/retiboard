"""
FastAPI application factory for RetiBoard.

Spec references:
  §2.2  — Vue SPA served locally at http://127.0.0.1:8787
  §4    — Pruning background job every 15 minutes
  §7    — Gossip sync engine background tasks
  §15   — Default port 8787
  §17   — No central servers

Design invariants:
  - Binds to 127.0.0.1 ONLY — no remote access.
  - CORS allows only localhost origins (the Vue SPA).
  - No authentication — the local machine IS the trust boundary.
  - Health endpoint exposes only structural info, never content.
  - Pruning + sync run as background tasks via FastAPI lifespan events.
"""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from retiboard.config import (
    APP_NAME,
    APP_VERSION,
    SPEC_VERSION,
    FRONTEND_DIST,
    API_PORT,
)

# Module-level reference to sync engine for lifespan access.
_sync_engine_ref = None


def create_app(
    relay_mode: bool = False,
    board_manager=None,
    sync_engine=None,
    identity=None,
    api_token: str = "",
) -> FastAPI:
    """
    Build and configure the FastAPI application.

    Args:
        relay_mode: If True, skip frontend serving (§2.2 relay mode).
        board_manager: The BoardManager instance for API routes.
        sync_engine: The SyncEngine instance for gossip (§7).
        api_token: Optional ephemeral API token for authentication.

    Returns:
        Configured FastAPI instance.
    """
    global _sync_engine_ref
    _sync_engine_ref = sync_engine

    app = FastAPI(
        title=APP_NAME,
        version=APP_VERSION,
        description="Sovereign imageboard over Reticulum (spec v{})".format(
            SPEC_VERSION
        ),
        docs_url="/docs" if not relay_mode else None,
        redoc_url=None,
        # Combined lifespan: pruning (§4) + sync engine (§7).
        lifespan=_combined_lifespan,
    )

    # -------------------------------------------------------------------------
    # CORS — localhost only (§2.2: SPA served on 127.0.0.1:8787)
    # -------------------------------------------------------------------------
    # During Vite dev mode the frontend runs on a different port (5173),
    # so we allow both the prod and dev origins.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            f"http://127.0.0.1:{API_PORT}",
            "http://127.0.0.1:5173",       # Vite dev server
            "http://localhost:5173",
            f"http://localhost:{API_PORT}",
        ],
        allow_credentials=False,            # No cookies, no sessions
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type", "X-RetiBoard-Token"],
    )

    # -------------------------------------------------------------------------
    # Security middleware (defense-in-depth, §2.2)
    # -------------------------------------------------------------------------
    from retiboard.api.middleware import SecurityHeadersMiddleware, APITokenMiddleware
    
    # Enforce API token if provided.
    if api_token:
        app.add_middleware(APITokenMiddleware, api_token=api_token)

    app.add_middleware(SecurityHeadersMiddleware)

    # -------------------------------------------------------------------------
    # Health endpoint — structural info only, never content (opacity)
    # -------------------------------------------------------------------------
    @app.get("/api/health")
    async def health():
        return {
            "status": "ok",
            "app": APP_NAME,
            "version": APP_VERSION,
            "spec": SPEC_VERSION,
            "relay_mode": relay_mode,
        }

    @app.post("/api/prune")
    async def trigger_prune():
        """Manually trigger a prune cycle. Returns structural summary."""
        from retiboard.pruning.pruner import prune_all_boards
        from dataclasses import asdict
        result = await prune_all_boards()
        return asdict(result)

    # -------------------------------------------------------------------------
    # API routers (must be registered BEFORE the SPA catch-all)
    # -------------------------------------------------------------------------
    if board_manager is not None:
        from retiboard.api.routes.boards import create_boards_router
        from retiboard.api.routes.posts import create_posts_router
        from retiboard.api.routes.sync import create_sync_router
        from retiboard.api.routes.status import create_status_router
        from retiboard.api.routes.moderation import create_moderation_router
        from retiboard.api.routes.settings import router as settings_router
        from retiboard.api.routes.logs import router as logs_router

        app.include_router(create_boards_router(board_manager))
        app.include_router(create_posts_router(board_manager, sync_engine, identity))
        app.include_router(create_sync_router(sync_engine))
        app.include_router(create_status_router(
            board_manager, sync_engine, relay_mode,
        ))
        app.include_router(create_moderation_router(sync_engine))
        app.include_router(settings_router, prefix="/api")
        app.include_router(logs_router, prefix="/api")

    # -------------------------------------------------------------------------
    # Static file serving — Vue SPA (skip in relay mode per §2.2)
    # -------------------------------------------------------------------------
    if not relay_mode:
        _mount_frontend(app)

    return app


@asynccontextmanager
async def _combined_lifespan(app):
    """
    Combined FastAPI lifespan: starts pruning (§4) + sync engine (§7).

    Both background systems start on app startup and are cleanly
    cancelled on shutdown.
    """
    from retiboard.pruning.scheduler import _prune_loop

    tasks = []

    # Start pruning background task (§4).
    prune_task = asyncio.create_task(_prune_loop())
    tasks.append(prune_task)

    # Start sync engine if available (§7).
    if _sync_engine_ref is not None:
        await _sync_engine_ref.start()

    import RNS
    RNS.log("Background tasks started (pruning + sync)", RNS.LOG_INFO)

    yield  # App is running.

    # Shutdown: stop sync engine and cancel prune task.
    if _sync_engine_ref is not None:
        await _sync_engine_ref.stop()

    for task in tasks:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    from retiboard.db.pool import get_pool
    await get_pool().close_all()

    RNS.log("Background tasks stopped", RNS.LOG_INFO)


def _mount_frontend(app: FastAPI) -> None:
    """
    Serve the built Vue SPA from frontend/dist/.

    The SPA is a single-page application: all non-API routes should
    return index.html so Vue Router handles client-side navigation.
    """
    dist = FRONTEND_DIST

    if not dist.exists():
        # Frontend not built yet — this is fine during early dev.
        # The /api/health endpoint still works; the user just gets a
        # 404 on the root until they run `npm run build`.
        import logging
        logging.getLogger("retiboard.api").warning(
            "Frontend dist/ not found at %s. "
            "Run 'cd frontend && npm run build' to build the SPA.",
            dist,
        )
        return

    # Serve static assets (JS, CSS, images) under /assets/
    assets_dir = dist / "assets"
    if assets_dir.exists():
        app.mount(
            "/assets",
            StaticFiles(directory=str(assets_dir)),
            name="static-assets",
        )

    # SPA fallback: any non-API route returns index.html
    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        """
        Catch-all route for the Vue SPA.
        API routes (/api/*) are registered before this, so they take
        priority. Everything else falls through to index.html.
        """
        # Check if the exact file exists in dist (e.g., favicon.ico)
        requested = dist / full_path
        if full_path and requested.exists() and requested.is_file():
            return FileResponse(str(requested))

        # Default: serve the SPA shell
        return FileResponse(str(dist / "index.html"))
