"""
Transport bandwidth detection for RetiBoard.

Spec references:
    §14.1 — "Reticulum supports transports with severely limited bandwidth,
             including LoRa links."
    §14.4 — "These behaviors are toggled manually in client settings or
             auto-detected based on RNS interface type."
    §7.2  — "Lower limits (max 2) on LoRa/slow interfaces (auto-detected
             via RNS)."
    §7.1 Tier 2 — "On low-bandwidth interfaces (detected via RNS link
                    speed < 10 kbit/s), the broadcast interval is increased
                    to 30-60 minutes and only the 10 most recent threads
                    are included."

This module is the SINGLE SOURCE OF TRUTH for transport awareness.
Every other module that needs to know "are we on LoRa?" calls functions
here rather than probing RNS directly.

Detection strategy:
    RNS exposes interface objects with a `bitrate` property (bits/sec).
    We scan all active interfaces and use the SLOWEST as the baseline —
    if any interface is LoRa, the node should behave conservatively to
    avoid overwhelming that link.

    The threshold for "low bandwidth" is 10 kbit/s (10000 bps), matching
    the spec's §7.1 Tier 2 language.

    A manual override (`RETIBOARD_LOW_BANDWIDTH=1`) is supported for
    cases where auto-detection fails or the user wants to force
    conservative behavior (§14.4: "toggled manually in client settings").

Payload size limits:
    Transport-aware payload size caps prevent large attachments from being
    attempted on constrained links. These are LOCAL enforcement —
    the backend rejects oversized payloads at the API boundary.

    Boards may also declare their own max_payload_size in the announce
    schema (sovereignty — the board creator decides). When both limits
    exist, the SMALLER of the two applies.
"""

from __future__ import annotations

import os
from typing import Optional

import RNS

from retiboard.config import (
    LOW_BANDWIDTH_THRESHOLD_BPS,
    MAX_PAYLOAD_SIZE_NORMAL,
    MAX_PAYLOAD_SIZE_LORA,
)


# =============================================================================
# Manual override
# =============================================================================

def _manual_override() -> Optional[bool]:
    """
    Check for manual low-bandwidth override via environment variable.

    RETIBOARD_LOW_BANDWIDTH=1  → force low-bandwidth mode
    RETIBOARD_LOW_BANDWIDTH=0  → force normal mode
    (unset)                    → auto-detect

    §14.4: "toggled manually in client settings"
    """
    val = os.environ.get("RETIBOARD_LOW_BANDWIDTH")
    if val is not None:
        return val.strip() in ("1", "true", "yes")
    return None


# =============================================================================
# Interface scanning
# =============================================================================

def _get_slowest_bitrate() -> Optional[int]:
    """
    Scan all active RNS interfaces and return the slowest bitrate (bps).

    Returns None if no interfaces are available or bitrate cannot be
    determined. RNS interfaces expose `bitrate` as an integer property
    representing bits per second.

    We check the slowest interface because if ANY link is constrained,
    gossip traffic on that link should be conservative. A node with
    both TCP and LoRa interfaces will see its LoRa peers suffer if
    HAVE packets are sized for TCP.
    """
    try:
        # RNS.Transport.interfaces is a list of active Interface objects.
        interfaces = RNS.Transport.interfaces
        if not interfaces:
            return None

        slowest = None
        for iface in interfaces:
            bitrate = getattr(iface, "bitrate", None)
            if bitrate is not None and isinstance(bitrate, (int, float)):
                bitrate = int(bitrate)
                if slowest is None or bitrate < slowest:
                    slowest = bitrate

        return slowest
    except Exception:
        return None


def _detect_interface_types() -> list[str]:
    """
    Return a list of interface type names for logging/debugging.

    Useful for status endpoints and log messages. Not used for
    decision-making — use is_low_bandwidth() for that.
    """
    try:
        interfaces = RNS.Transport.interfaces
        return [type(iface).__name__ for iface in interfaces]
    except Exception:
        return []


# =============================================================================
# Public API
# =============================================================================

def is_low_bandwidth() -> bool:
    """
    Determine if this node is operating on a low-bandwidth transport.

    Returns True if:
      - Manual override is set (RETIBOARD_LOW_BANDWIDTH=1), OR
      - The slowest active RNS interface has bitrate < 10 kbit/s

    This is the primary predicate used by:
      - HAVE loop (§7.1 Tier 2): 10-thread cap, 30-60 min interval
      - Rate limiter (§7.2): max 2 concurrent syncs
      - Payload size enforcement: tighter limit
      - Board subscription defaults: prefer text-only

    The result is NOT cached because interfaces can change at runtime
    (e.g., LoRa interface comes online after boot). Each caller gets
    a fresh reading.
    """
    # 1. Check manual override.
    override = _manual_override()
    if override is not None:
        return override

    # 2. Auto-detect from RNS interfaces.
    slowest = _get_slowest_bitrate()
    if slowest is None:
        # Can't determine bitrate — assume normal bandwidth.
        # This is the safe default: normal mode is less aggressive
        # about throttling, and if we're wrong the node will just
        # generate more gossip traffic than ideal.
        return False

    return slowest < LOW_BANDWIDTH_THRESHOLD_BPS


def get_max_payload_size(board_max_payload_size: int = 0) -> int:
    """
    Return the effective maximum payload size in bytes.

    Considers both transport limits and board-declared limits.
    The smaller of the two applies.

    Args:
        board_max_payload_size: Board's declared max_payload_size from
            the announce schema. 0 means no board-level limit.

    Returns:
        Maximum payload size in bytes.
    """
    transport_limit = (
        MAX_PAYLOAD_SIZE_LORA if is_low_bandwidth()
        else MAX_PAYLOAD_SIZE_NORMAL
    )

    if board_max_payload_size > 0:
        return min(transport_limit, board_max_payload_size)

    return transport_limit


def get_transport_info() -> dict:
    """
    Return transport status information for the status API.

    Exposes interface types, slowest bitrate, and low-bandwidth state.
    No content, no keys — purely structural (opacity preserved).
    """
    slowest = _get_slowest_bitrate()
    return {
        "is_low_bandwidth": is_low_bandwidth(),
        "slowest_bitrate_bps": slowest,
        "interface_types": _detect_interface_types(),
        "max_payload_size": get_max_payload_size(),
        "manual_override": _manual_override(),
    }
