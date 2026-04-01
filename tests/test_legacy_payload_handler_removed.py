from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def test_engine_no_longer_registers_legacy_payload_handler() -> None:
    engine_source = Path('retiboard/sync/engine.py').read_text()
    assert 'payload_request_handler' not in engine_source
    assert 'PATH_PAYLOAD' not in engine_source


def test_payload_fetch_no_legacy_payload_request_handler() -> None:
    source = Path('retiboard/sync/payload_fetch.py').read_text()
    assert 'def payload_request_handler' not in source
