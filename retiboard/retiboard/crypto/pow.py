"""
Proof-of-Work solver and verifier for RetiBoard.

Spec references:
    §11.1 — "hash(metadata + nonce) < difficulty_target"
            "Difficulty is per-board, declared in the board announce."
            "Verification is cheap; solving scales with difficulty."
            "PoW applies uniformly to every post (OP and replies)."
    §11.2 — "Clients reject metadata that fails PoW verification."
            "Gossip peers do not propagate metadata with invalid PoW."

Design invariants:
    - PoW operates on structural METADATA only — never on content/payload.
    - The backend never sees plaintext; content_hash is over ciphertext.
    - difficulty=0 means PoW is completely skipped (trusted/private boards).
    - The canonical metadata representation for hashing must be deterministic
      (sorted keys, no whitespace) to ensure cross-node consistency.

Algorithm:
    1. Canonicalize metadata fields (sorted JSON, no whitespace)
    2. Concatenate: canonical_metadata + nonce_string
    3. Hash: SHA-256(concatenation)
    4. Compare: hash_int < difficulty_target
       where difficulty_target = 2^(256 - difficulty)
       i.e., the hash must have at least `difficulty` leading zero bits.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import Optional


# Fields included in the PoW hash. Must match what the frontend uses.
# Excludes: pow_nonce (that's what we're solving for),
#           thread_last_activity, is_abandoned, expiry_timestamp (local-only).
_POW_FIELDS = [
    "post_id",
    "thread_id",
    "parent_id",
    "timestamp",
    "bump_flag",
    "content_hash",
    "payload_size",
    "attachment_content_hash",
    "attachment_payload_size",
    "has_attachments",
    "attachment_count",
    "text_only",
    "identity_hash",
    "public_key",
    "encrypted_pings",
    "edit_signature",
]


def canonicalize_metadata(metadata: dict) -> str:
    """
    Produce a deterministic string representation of post metadata
    for PoW hashing.

    Only the fields in _POW_FIELDS are included, sorted by key name.
    Values are JSON-serialized with no whitespace.

    This must be identical on every node and in the frontend.

    Args:
        metadata: Dict with at least the _POW_FIELDS keys.

    Returns:
        Deterministic JSON string (sorted keys, compact separators).
    """
    canonical = {}
    for key in _POW_FIELDS:
        if key not in metadata:
            continue
        value = metadata[key]
        if key == "encrypted_pings":
            if isinstance(value, list):
                value = sorted(item for item in value if isinstance(item, str))
            else:
                value = []
        canonical[key] = value
    return json.dumps(canonical, sort_keys=True, separators=(",", ":"))


def compute_pow_hash(canonical_metadata: str, nonce: str) -> str:
    """
    Compute the PoW hash: SHA-256(canonical_metadata + nonce).

    Args:
        canonical_metadata: Deterministic metadata string from canonicalize_metadata().
        nonce: The nonce string being tested.

    Returns:
        SHA-256 hex digest.
    """
    preimage = (canonical_metadata + nonce).encode("utf-8")
    return hashlib.sha256(preimage).hexdigest()


def difficulty_target(difficulty: int) -> int:
    """
    Compute the numeric target for a given difficulty level.

    The PoW hash (as a 256-bit integer) must be less than this target.

    difficulty=0: target = 2^256 (always passes)
    difficulty=1: hash must start with 1 leading zero bit
    difficulty=N: hash must start with N leading zero bits

    Returns:
        Integer target value.
    """
    if difficulty <= 0:
        return 2 ** 256  # Everything passes
    return 2 ** (256 - difficulty)


def verify_pow(metadata: dict, nonce: str, difficulty: int) -> bool:
    """
    Verify that a PoW solution is valid.

    Per §11.1: "Verification is cheap."

    Args:
        metadata: Post metadata dict (must contain _POW_FIELDS).
        nonce: The claimed solution nonce.
        difficulty: Board's PoW difficulty (0 = skip entirely).

    Returns:
        True if the PoW is valid (or difficulty=0), False otherwise.
    """
    # §11.1: difficulty=0 → no PoW required (trusted/private boards).
    if difficulty <= 0:
        return True

    canonical = canonicalize_metadata(metadata)
    hash_hex = compute_pow_hash(canonical, nonce)
    hash_int = int(hash_hex, 16)
    target = difficulty_target(difficulty)

    return hash_int < target


def solve_pow(
    metadata: dict,
    difficulty: int,
    max_iterations: int = 10_000_000,
) -> Optional[str]:
    """
    Solve PoW for a piece of metadata.

    This is CPU-bound and scales with difficulty (§11.1).
    The frontend normally calls this, but we provide a backend
    solver for testing and relay-mode posting.

    Args:
        metadata: Post metadata dict.
        difficulty: Required difficulty.
        max_iterations: Safety cap to prevent infinite loops.

    Returns:
        A valid nonce string, or None if max_iterations exceeded.
    """
    if difficulty <= 0:
        return ""  # No work needed

    canonical = canonicalize_metadata(metadata)
    target = difficulty_target(difficulty)

    for _ in range(max_iterations):
        nonce = secrets.token_hex(8)  # 16-char random hex nonce
        hash_hex = compute_pow_hash(canonical, nonce)
        hash_int = int(hash_hex, 16)

        if hash_int < target:
            return nonce

    return None  # Failed to find solution within iteration cap


def verify_content_hash(data: bytes, expected_hash: str) -> bool:
    """
    Verify that a payload blob's SHA-256 matches the expected content_hash.

    Per §6.2: "Verify content_hash matches."
    The hash is over the encrypted blob (nonce + ciphertext + tag),
    NOT the plaintext. The backend never sees the plaintext.

    Args:
        data: Raw encrypted payload bytes.
        expected_hash: SHA-256 hex from the post metadata.

    Returns:
        True if the hash matches.
    """
    computed = hashlib.sha256(data).hexdigest()
    return computed == expected_hash
