import json
import random
import string
import pytest
from unittest.mock import MagicMock

from retiboard.sync.receiver import make_delivery_callback

def random_string(length=10):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

def random_json(depth=3):
    if depth <= 0:
        return random.choice([random_string(), random.randint(0, 100), True, False, None])
    
    choice = random.choice(['dict', 'list', 'atom'])
    if choice == 'dict':
        return {random_string(3): random_json(depth-1) for _ in range(random.randint(1, 3))}
    elif choice == 'list':
        return [random_json(depth-1) for _ in range(random.randint(1, 3))]
    else:
        return random_json(0)

@pytest.fixture
def mock_sync_engine():
    engine = MagicMock()
    engine._loop = MagicMock()
    return engine

@pytest.fixture
def mock_peer_tracker():
    return MagicMock()

def test_fuzz_lxmf_receiver(mock_sync_engine, mock_peer_tracker):
    callback = make_delivery_callback(mock_peer_tracker, mock_sync_engine)
    
    # List of known message types to fuzz
    message_types = [
        "METADATA", "HAVE", "HAVE_REQ", "DELTA_REQ", "DELTA_RES", 
        "PAYLOAD_REQ", "PAYLOAD_RES", "BOARD_ANNOUNCE",
        "CHUNK_MANIFEST_REQ", "CHUNK_MANIFEST_RES", "CHUNK_MANIFEST_UNAV",
        "CHUNK_REQ", "CHUNK_CANCEL", "CHUNK_OFFER"
    ]
    
    for _ in range(100):
        # 1. Random message type (valid or invalid)
        title = random.choice(message_types + [random_string(5)])
        
        # 2. Random content (valid JSON, invalid JSON, or garbage)
        content_type = random.choice(['valid_json', 'invalid_json', 'garbage'])
        if content_type == 'valid_json':
            content = json.dumps(random_json())
        elif content_type == 'invalid_json':
            content = json.dumps(random_json())[:-1] # Cut off last char
        else:
            content = random_string(100)
            
        # 3. Mock message object
        message = MagicMock()
        message.title = title.encode('utf-8')
        message.content = content.encode('utf-8')
        message.source_hash = b'dummy_source_hash_32_bytes_long__'[:32]
        message.source = MagicMock()
        message.source.identity = None
        
        # 4. Call callback
        # Should not raise any unhandled exceptions
        try:
            callback(message)
        except Exception as e:
            pytest.fail(f"Fuzzer triggered unhandled exception in delivery_callback: {e}")

def test_fuzz_api_create_post():
    import io
    import os
    import shutil
    import tempfile
    from fastapi.testclient import TestClient
    from retiboard.api.routes.posts import create_posts_router
    from retiboard.db.database import open_board_db, save_board_config
    from retiboard.db.models import Board
    from fastapi import FastAPI
    import asyncio

    # Setup temp home
    test_home = tempfile.mkdtemp(prefix="retiboard_fuzz_api_")
    os.environ["RETIBOARD_HOME"] = test_home
    
    try:
        board_id = "fuzzboard"
        board = Board(
            board_id=board_id,
            display_name="Fuzz Board",
            text_only=False,
            default_ttl_seconds=43200,
            bump_decay_rate=3600,
            pow_difficulty=0,
            key_material="deadbeef" * 4,
            announce_version=2,
        )

        async def _setup():
            db = await open_board_db(board_id)
            try:
                await save_board_config(db, board)
            finally:
                await db.close()
        
        asyncio.run(_setup())

        app = FastAPI()
        app.include_router(create_posts_router(board_manager=None, sync_engine=None))
        
        client = TestClient(app)

        for _ in range(50):
            # Random metadata (valid JSON but random fields or invalid JSON)
            if random.random() < 0.8:
                metadata = random_json()
            else:
                metadata = random_string(100)
            
            # Random payload
            payload = os.urandom(random.randint(0, 100))
            
            files = {
                "payload": ("payload.bin", io.BytesIO(payload), "application/octet-stream"),
            }
            
            # Randomly add attachment_payload
            if random.random() < 0.3:
                attachment = os.urandom(random.randint(0, 100))
                files["attachment_payload"] = ("attachment.bin", io.BytesIO(attachment), "application/octet-stream")

            # The API should NEVER return 500, always 4xx for bad input
            metadata_str = json.dumps(metadata) if isinstance(metadata, (dict, list)) else str(metadata)
            response = client.post(
                f"/api/boards/{board_id}/posts",
                data={"metadata": metadata_str},
                files=files
            )
            
            assert response.status_code < 500, f"API returned 500 for metadata: {metadata_str}"

    finally:
        shutil.rmtree(test_home, ignore_errors=True)

@pytest.mark.asyncio
async def test_fuzz_chunk_control():
    import os
    import shutil
    import tempfile
    from unittest.mock import MagicMock
    from retiboard.sync.payload_fetch import (
        handle_chunk_manifest_request_lxmf,
        handle_chunk_manifest_response_lxmf,
        handle_chunk_manifest_unavailable_lxmf,
        handle_chunk_offer_lxmf,
        handle_chunk_request_lxmf,
        handle_chunk_cancel_lxmf
    )
    
    test_home = tempfile.mkdtemp(prefix="retiboard_fuzz_chunks_")
    os.environ["RETIBOARD_HOME"] = test_home
    
    try:
        mock_engine = MagicMock()
        mock_engine.send_lxmf = MagicMock()
        mock_engine.get_lxmf_hash = MagicMock(return_value="local_hash")
        
        source_hash = b"peer_hash_32_bytes_long_12345678"
        source_identity = MagicMock()
        
        # Helper to generate "evil" chunk-control payloads
        def generate_evil_chunk_payload(msg_type):
            base = {
                "board_id": random.choice(["fuzzboard", "", random_string(100)]),
                "blob_hash": random.choice(["a"*64, "", random_string(100)]),
            }
            
            if msg_type == "manifest_req":
                return base
            
            if msg_type == "manifest_res":
                base.update({
                    "blob_size": random.choice([0, -1, 10**12, 1024]),
                    "chunk_size": random.choice([0, -1, 10**6, 1024]),
                    "chunk_count": random.choice([0, -1, 10**6, 10]),
                    "blob_kind": random.choice(["text", "attachments", "garbage"]),
                    "entries": [
                        {
                            "blob_hash": random_string(64),
                            "chunk_index": random.choice([0, -1, 10**6]),
                            "offset": random.choice([0, -1, 10**12]),
                            "size": random.choice([0, -1, 10**12]),
                            "chunk_hash": random_string(64)
                        } for _ in range(random.randint(0, 5))
                    ]
                })
                return base

            if msg_type == "manifest_unav":
                base["reason"] = random.choice(["not_found", "pruned", "abandoned", "garbage", ""])
                return base
                
            if msg_type == "offer":
                base.update({
                    "chunk_count": random.choice([0, -1, 10**6]),
                    "complete": random.choice([True, False, "garbage"]),
                    "ranges": [
                        [random.randint(-10, 10), random.randint(-10, 10)] 
                        for _ in range(random.randint(0, 5))
                    ]
                })
                return base
                
            if msg_type == "req":
                base.update({
                    "request_id": random_string(32),
                    "chunk_index": random.choice([0, -1, 10**6])
                })
                return base
                
            if msg_type == "cancel":
                return {"request_id": random.choice([random_string(32), "", None])}
                
            return base

        handlers = [
            (handle_chunk_manifest_request_lxmf, "manifest_req"),
            (handle_chunk_manifest_response_lxmf, "manifest_res"),
            (handle_chunk_manifest_unavailable_lxmf, "manifest_unav"),
            (handle_chunk_offer_lxmf, "offer"),
            (handle_chunk_request_lxmf, "req"),
            (handle_chunk_cancel_lxmf, "cancel")
        ]

        for _ in range(100):
            handler, payload_type = random.choice(handlers)
            payload = generate_evil_chunk_payload(payload_type)
            content = json.dumps(payload).encode("utf-8")
            
            # Should not crash
            try:
                if handler in [handle_chunk_manifest_request_lxmf, handle_chunk_request_lxmf, handle_chunk_cancel_lxmf]:
                    await handler(content, source_hash, source_identity, mock_engine)
                else:
                    await handler(content, source_hash)
            except Exception as e:
                # We expect some logged errors, but not unhandled exceptions that propagate up
                # unless they are part of the intentional logic (which shouldn't happen for valid handlers)
                pytest.fail(f"Handler {handler.__name__} failed with {type(e).__name__}: {e} for payload {payload}")

    finally:
        shutil.rmtree(test_home, ignore_errors=True)
