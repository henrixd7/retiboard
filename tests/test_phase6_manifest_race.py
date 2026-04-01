"""Phase 6 manifest race regression tests.

These tests require the runtime transport dependency stack. They self-skip in
minimal CI containers where RNS is unavailable.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

pytest.importorskip("RNS")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from retiboard.sync.payload_fetch import (  # noqa: E402
    cancel_pending_chunk_session,
    handle_chunk_manifest_unavailable_lxmf,
    register_pending_chunk_session,
)


@pytest.mark.asyncio
async def test_pruned_manifest_unavailable_is_peer_scoped() -> None:
    loop = asyncio.get_running_loop()
    session = register_pending_chunk_session("b", "blob", "attachments", "peer-a", loop)
    session.candidate_peer_lxmf_hashes = ["peer-a", "peer-b"]
    try:
        await handle_chunk_manifest_unavailable_lxmf(
            json.dumps({"blob_hash": "blob", "reason": "pruned"}),
            "peer-a",
        )
        assert session.failed_event.is_set() is False
        assert session.manifest_unavailable_by_peer == {"peer-a": "pruned"}
    finally:
        await cancel_pending_chunk_session("blob")


@pytest.mark.asyncio
async def test_withheld_local_policy_manifest_unavailable_is_peer_scoped() -> None:
    loop = asyncio.get_running_loop()
    session = register_pending_chunk_session("b", "blob2", "attachments", "peer-a", loop)
    session.candidate_peer_lxmf_hashes = ["peer-a", "peer-b"]
    try:
        await handle_chunk_manifest_unavailable_lxmf(
            json.dumps({"blob_hash": "blob2", "reason": "withheld_local_policy"}),
            "peer-a",
        )
        assert session.failed_event.is_set() is False
        assert session.unavailable_reason == ""
        assert session.manifest_unavailable_by_peer == {"peer-a": "withheld_local_policy"}
    finally:
        await cancel_pending_chunk_session("blob2")
