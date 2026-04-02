"""Memory-efficient reassembly of a verified encrypted blob."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


class ReassemblyBuffer:
    """Sparse/random-access assembler for an encrypted blob.

    True zero-copy is not realistic in Python here. The correct Phase 1 design
    is receive -> hash -> direct random-access file write -> final stream hash.
    """

    def __init__(self, temp_blob_path: Path, blob_size: int, chunk_count: int):
        self.temp_blob_path = Path(temp_blob_path)
        self.blob_size = blob_size
        self.chunk_count = chunk_count
        self._present: set[int] = set()

    def reserve(self) -> None:
        self.temp_blob_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.temp_blob_path, "wb") as handle:
            if self.blob_size > 0:
                handle.truncate(self.blob_size)

    def write_verified_chunk(self, chunk_index: int, offset: int, data: bytes) -> None:
        with open(self.temp_blob_path, "r+b") as handle:
            handle.seek(offset)
            handle.write(data)
        self._present.add(chunk_index)

    def read_chunk(self, offset: int, size: int) -> bytes | None:
        """Read one chunk-sized region from the assembly file."""
        if not self.temp_blob_path.exists():
            return None
        try:
            with open(self.temp_blob_path, "rb") as handle:
                handle.seek(offset)
                data = handle.read(size)
        except OSError:
            return None
        if len(data) != size:
            return None
        return data

    def mark_present(self, chunk_index: int) -> None:
        self._present.add(chunk_index)

    def verify_chunk_on_disk(
        self,
        chunk_index: int,
        offset: int,
        size: int,
        expected_hash: str,
    ) -> bool:
        """Verify one chunk region already written to the assembly file.

        Used during session restore so persisted "stored" state is only
        trusted if the corresponding on-disk bytes still match the manifest.
        """
        _ = chunk_index  # Structural symmetry with other chunk APIs.
        data = self.read_chunk(offset, size)
        if data is None:
            return False
        return hashlib.sha256(data).hexdigest() == expected_hash

    def is_complete(self) -> bool:
        return len(self._present) >= self.chunk_count

    def finalize(self, expected_blob_hash: str, final_path: Path) -> None:
        computed = hashlib.sha256()
        with open(self.temp_blob_path, "rb") as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                computed.update(block)

        digest = computed.hexdigest()
        if digest != expected_blob_hash:
            raise ValueError(
                f"Assembled blob hash mismatch: expected {expected_blob_hash}, got {digest}"
            )

        final_path = Path(final_path)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(self.temp_blob_path, final_path)
        except FileNotFoundError:
            # Another concurrent identical session may already have finalized.
            if not final_path.exists():
                raise
