"""
RNS Identity management for RetiBoard.

Spec references:
  §12.1 — Reticulum native identities (public/private keypairs)
  §2.2  — Each client is its own node; identity stored user-locally

Design invariants:
  - Identity file lives at ~/.retiboard/identity (sovereign, never shared).
  - The raw private key NEVER leaves this module or the local filesystem.
  - Only the identity hash (public key hash) is used in metadata (§12.3).

RNS API (verified against reticulum.network/manual/reference.html):
  - RNS.Identity(create_keys=True) creates a new identity with fresh keypair.
  - RNS.Identity.from_file(path) — STATIC METHOD — loads identity from file.
  - identity.to_file(path) persists the keypair to disk.
  - identity.hash is the truncated hash of the public key (bytes).
  - identity.hexhash is the hex-string representation.
"""

import RNS

from retiboard.config import IDENTITY_PATH, RETIBOARD_HOME


def ensure_data_dirs() -> None:
    """Create the ~/.retiboard/ directory tree if it doesn't exist."""
    RETIBOARD_HOME.mkdir(parents=True, exist_ok=True)


def load_or_create_identity() -> RNS.Identity:
    """
    Load an existing RNS identity from disk, or create a new one.

    Returns:
        RNS.Identity — the node's persistent identity.

    The identity file is stored at IDENTITY_PATH (~/.retiboard/identity).
    On first run, a new keypair is generated and saved. On subsequent
    runs, the existing keypair is loaded so the node maintains a stable
    identity across sessions.

    Security note: The private key component is stored in this file.
    File permissions should be restricted (0600). We do NOT enforce
    this programmatically to respect sovereignty — the user controls
    their own filesystem.
    """
    ensure_data_dirs()

    identity_file = str(IDENTITY_PATH)

    if IDENTITY_PATH.exists():
        # Load existing identity from disk.
        RNS.log(
            f"Loading existing identity from {identity_file}",
            RNS.LOG_INFO,
        )
        identity = RNS.Identity.from_file(identity_file)
        RNS.log(
            f"Identity loaded: {identity.hexhash}",
            RNS.LOG_INFO,
        )
    else:
        # First run — generate a new identity and persist it.
        RNS.log(
            "No existing identity found. Generating new keypair...",
            RNS.LOG_INFO,
        )
        identity = RNS.Identity()
        identity.to_file(identity_file)
        RNS.log(
            f"New identity created and saved: {identity.hexhash}",
            RNS.LOG_INFO,
        )
        RNS.log(
            f"Identity file: {identity_file}",
            RNS.LOG_DEBUG,
        )

    return identity


def get_identity_hash(identity: RNS.Identity) -> str:
    """
    Return the identity hash as a hex string (§12.3).

    This is the ONLY representation of identity that appears in post
    metadata. It is a hash of the public key — not the key itself.
    Used solely for client-side filtering and blocking.
    """
    return identity.hexhash
