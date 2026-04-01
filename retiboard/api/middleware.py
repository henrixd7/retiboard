"""
Security middleware for RetiBoard API.

Spec references:
    §2.2  — "Vue SPA served locally at http://127.0.0.1:8787"
    §17   — "No central servers"

Design invariants:
    - The HTTP API is bound to 127.0.0.1 ONLY (enforced in main.py/uvicorn).
    - This middleware adds defense-in-depth security headers.
    - No authentication — the local machine IS the trust boundary.
    - No cookies, no sessions, no tokens.

Security headers added:
    - X-Content-Type-Options: nosniff — prevent MIME sniffing of payloads
    - X-Frame-Options: DENY — prevent iframe embedding
    - Referrer-Policy: no-referrer — prevent referrer leaking
    - Cache-Control: no-store — prevent caching of sensitive API responses
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, JSONResponse


class APITokenMiddleware(BaseHTTPMiddleware):
    """
    Enforce high-entropy ephemeral API token authentication.

    All requests to /api/ (except /api/health) must include the
    X-RetiBoard-Token header matching the token generated at startup.
    """

    def __init__(self, app, api_token: str):
        super().__init__(app)
        self.api_token = api_token

    async def dispatch(self, request: Request, call_next) -> Response:
        # Static files and non-API routes are always permitted.
        # This allows the SPA to load and capture the token from the URL.
        if not request.url.path.startswith("/api/"):
            return await call_next(request)

        # /api/health is permitted without a token for monitoring.
        if request.url.path == "/api/health":
            return await call_next(request)

        # Check for token in header.
        token_header = request.headers.get("X-RetiBoard-Token")
        if token_header == self.api_token:
            return await call_next(request)

        # Unauthorized: return a clear JSON error.
        return JSONResponse(
            status_code=401,
            content={
                "detail": "Unauthorized: API token missing or invalid. "
                          "Please use the secure URL provided in the terminal."
            }
        )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Add security headers to all API responses.

    These are defense-in-depth measures. The primary security boundary
    is the localhost binding — these headers protect against browser
    quirks and local attack vectors.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)

        # Prevent MIME type sniffing (critical for opaque payload serving).
        response.headers["X-Content-Type-Options"] = "nosniff"

        # Prevent clickjacking via iframe embedding.
        response.headers["X-Frame-Options"] = "DENY"

        # Don't leak referrer information.
        response.headers["Referrer-Policy"] = "no-referrer"

        # API responses should never be cached by the browser.
        # Payload blobs could be cached, but we err on the side of
        # privacy for now. The frontend has its own LRU cache (§10).
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"

        return response
