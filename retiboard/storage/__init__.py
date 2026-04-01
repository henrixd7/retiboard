"""
RetiBoard payload storage module.

Opaque encrypted blob storage. Content-blind by design.
See payloads.py for the implementation.

Spec: §3.2, §4
"""

from retiboard.storage.payloads import (
    payload_path,
    write_payload,
    read_payload,
    delete_payload,
    delete_payloads_bulk,
    payload_exists,
    get_payload_size,
    delete_chunk_cache,
    delete_chunk_cache_bulk,
)

__all__ = [
    "payload_path",
    "write_payload",
    "read_payload",
    "delete_payload",
    "delete_payloads_bulk",
    "payload_exists",
    "get_payload_size",
    "delete_chunk_cache",
    "delete_chunk_cache_bulk",
]
