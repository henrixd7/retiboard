# ruff: noqa: E402

"""
Phase 10 — End-to-End Gossip & Sync Test Suite (Subprocess Version).

Simulates a real network by spawning two independent RetiBoard processes.
Verifies:
    1. Board Creation
    2. Peer Registration via API
    3. Metadata Sync (HAVE/DELTA)
    4. Payload Fetch
"""

import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

# Use fixed ports for test stability.
ALICE_PORT = 8788
BOB_PORT = 8789

class NodeProcess:
    def __init__(self, name: str, port: int):
        self.name = name
        self.port = port
        self.home = Path(tempfile.mkdtemp(prefix=f"retiboard_e2e_v2_{name}_"))
        self.process = None
        self.token = None
        self.base_url = f"http://127.0.0.1:{port}"

    def start(self):
        env = os.environ.copy()
        env["RETIBOARD_HOME"] = str(self.home)
        
        # We use -u for unbuffered output to catch the token immediately.
        cmd = [sys.executable, "-u", "-m", "retiboard", "--port", str(self.port), "--log-to-console"]
        print(f"[*] Starting {self.name}: {' '.join(cmd)}")
        self.process = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1, # Line buffered
            preexec_fn=os.setsid if os.name != 'nt' else None
        )
        
        # Wait for the token in stdout.
        start_time = time.time()
        while time.time() - start_time < 30:
            line = self.process.stdout.readline()
            if not line:
                break
            # print(f"[{self.name}] {line.strip()}")
            
            # Looking for: http://127.0.0.1:8788?token=...
            match = re.search(r"token=([A-Za-z0-9_-]+)", line)
            if match:
                self.token = match.group(1)
                print(f"[*] Node '{self.name}' started. Token: {self.token}")
                return True
        
        # Check if process died
        ret = self.process.poll()
        if ret is not None:
            print(f"[!] {self.name} died with exit code {ret}")
            # Try to read some more output
            out, _ = self.process.communicate(timeout=1)
            print(f"[!] Output:\n{out}")
        
        self.stop()
        raise Exception(f"Failed to start node '{self.name}' or capture token")

    def stop(self):
        if self.process:
            if os.name != 'nt':
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            else:
                self.process.terminate()
            self.process.wait()
        shutil.rmtree(self.home, ignore_errors=True)

    async def api_get(self, path: str):
        print(f"[*] {self.name} GET {path}")
        headers = {"X-RetiBoard-Token": self.token}
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{self.base_url}{path}", headers=headers, timeout=10.0)
            r.raise_for_status()
            return r.json()

    async def api_post(self, path: str, json_data: dict = None, files: dict = None, data: dict = None):
        print(f"[*] {self.name} POST {path}")
        headers = {"X-RetiBoard-Token": self.token}
        async with httpx.AsyncClient() as client:
            r = await client.post(f"{self.base_url}{path}", headers=headers, json=json_data, files=files, data=data, timeout=10.0)
            r.raise_for_status()
            return r.json()

async def run_e2e():
    alice = NodeProcess("Alice", ALICE_PORT)
    bob = NodeProcess("Bob", BOB_PORT)
    
    try:
        alice.start()
        bob.start()
        
        # Give them a moment to initialize RNS.
        await asyncio.sleep(5)
        
        # 1. Get Identities.
        alice_id = await alice.api_get("/api/identity")
        bob_id = await bob.api_get("/api/identity")
        
        print(f"[*] Alice LXMF: {alice_id['lxmf_hash']}")
        print(f"[*] Bob LXMF: {bob_id['lxmf_hash']}")
        
        # 2. Alice creates a board.
        print("[*] Alice creating board...")
        board = await alice.api_post("/api/boards", json_data={
            "display_name": "E2E Optimized Board",
            "pow_difficulty": 0
        })
        board_id = board["board_id"]
        print(f"[*] Board created: {board_id}")
        
        # 3. Register peers manually.
        print("[*] Registering Bob as peer for Alice...")
        await alice.api_post("/api/peers", json_data={
            "peer_lxmf_hash": bob_id["lxmf_hash"],
            "public_key": bob_id["public_key"],
            "board_id": board_id
        })
        
        print("[*] Registering Alice as peer for Bob...")
        await bob.api_post("/api/peers", json_data={
            "peer_lxmf_hash": alice_id["lxmf_hash"],
            "public_key": alice_id["public_key"],
            "board_id": board_id
        })
        
        # 4. Bob subscribes to the board.
        print("[*] Bob subscribing to board...")
        # Since we are not doing a real announce discovery, we "force" subscribe
        # by giving Bob the board details.
        # We don't have a direct "import board" API, but we can simulate discovery
        # if the announce handler was running. 
        # For this test, let's just use Alice's config to "pre-seed" Bob.
        # Actually, let's just make Bob subscribe if he sees it in "discovered".
        # But discovery takes time. Let's just create a subscription endpoint
        # or use the existing one if it allows arbitrary board_id.
        # The existing POST /api/boards/{board_id}/subscribe requires it to be in discovered_boards.
        
        # Let's just have Alice announce it and Bob wait to see it.
        # Or simpler: Alice creates a post, which triggers a HAVE broadcast.
        
        # 5. Alice creates a post.
        print("[*] Alice creating post...")
        post_id = "p_e2e_" + os.urandom(4).hex()
        content = b"E2E Optimization Test Payload"
        content_hash = hashlib.sha256(content).hexdigest()
        
        now = int(time.time())
        metadata = {
            "post_id": post_id,
            "thread_id": post_id,
            "timestamp": now,
            "bump_flag": True,
            "content_hash": content_hash,
            "payload_size": len(content),
            "text_only": True
        }
        
        await alice.api_post(
            f"/api/boards/{board_id}/posts",
            data={"metadata": json.dumps(metadata)},
            files={"payload": ("payload.bin", content)}
        )
        
        # 6. Bob subscribes (he needs the board config first).
        # We'll use a sneaky way: Bob "discovers" it because Alice announced it.
        print("[*] Bob waiting for board discovery...")
        discovered = []
        for _ in range(30):
            res = await bob.api_get("/api/boards/discovered")
            discovered = res["boards"]
            if any(b["board_id"] == board_id for b in discovered):
                break
            await asyncio.sleep(1)
        
        if not any(b["board_id"] == board_id for b in discovered):
            raise Exception("Bob never discovered Alice's board")
            
        print("[*] Bob subscribing...")
        await bob.api_post(f"/api/boards/{board_id}/subscribe")
        
        # 6.5 Request catchup to force sync
        print("[*] Bob requesting catchup...")
        await bob.api_post(f"/api/boards/{board_id}/control/request-catchup")
        
        # 7. Wait for sync.
        print("[*] Waiting for Bob to sync post...")
        synced = False
        for i in range(60):
            try:
                # Check status
                status_alice = await alice.api_get("/api/status")
                status_bob = await bob.api_get("/api/status")
                if i % 5 == 0:
                    print(f"[*] Status: Alice peers={status_alice['total_peers']} Bob peers={status_bob['total_peers']}")
                    print(f"[*] Alice Msg Queue: {status_alice['message_queue_depth']} Bob Delta Queue: {status_bob['delta_queue_size']}")

                # Check Bob's catalog for the board.
                catalog = await bob.api_get(f"/api/boards/{board_id}/posts")
                if any(t["thread_id"] == post_id for t in catalog):
                    print("[*] Bob received thread metadata! Fetching payload...")
                    # Check if payload is there too.
                    try:
                        headers = {"X-RetiBoard-Token": bob.token}
                        async with httpx.AsyncClient() as client:
                            r = await client.get(f"{bob.base_url}/api/boards/{board_id}/payloads/{content_hash}", headers=headers)
                            if r.status_code == 200 and r.content == content:
                                synced = True
                                break
                            else:
                                print(f"[*] Payload not ready yet (status {r.status_code})")
                    except Exception as e:
                        print(f"[*] Payload fetch failed: {e}")
                else:
                    if i % 5 == 0:
                        print(f"[*] Bob catalog: {[t['thread_id'] for t in catalog]}")
            except Exception as e:
                print(f"[*] Sync check error: {e}")
            await asyncio.sleep(2)
            
        if synced:
            print("[+] E2E SUCCESS: Alice -> Bob sync complete!")
        else:
            raise Exception("E2E FAILURE: Sync timed out or data mismatch")

        # 8. Alice purges the thread.
        print(f"[*] Alice purging thread {post_id}...")
        await alice.api_post(f"/api/boards/{board_id}/control/purge-thread", json_data={"thread_id": post_id})
        
        # Verify Alice purged it
        catalog_alice = await alice.api_get(f"/api/boards/{board_id}/posts")
        if any(t["thread_id"] == post_id for t in catalog_alice):
            raise Exception("Alice failed to purge thread locally")
        print("[*] Alice purged thread locally.")

        # 9. Bob requests catchup and should discover the purge (implicitly via missing HAVE or empty HAVE).
        # Actually, in RetiBoard, a purge is a local tombstone. 
        # For Bob to "know" it's purged on Alice, he just won't see it in her HAVE anymore.
        # If Bob also triggers a prune/purge cycle, it should disappear if it expired,
        # but here we are testing manual purge consistency if possible.
        # RetiBoard §19: "moderation is local-only". 
        # So Bob won't automatically purge just because Alice did, UNLESS he also purges it.
        # However, we can test that Bob can NO LONGER fetch the payload from Alice.
        
        print("[*] Bob attempting to fetch purged payload from Alice...")
        # We'll clear Bob's local payload first to force a re-fetch if he wanted to.
        # But wait, Bob already has it. Let's just verify Alice no longer serves it.
        headers = {"X-RetiBoard-Token": alice.token}
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{alice.base_url}/api/boards/{board_id}/payloads/{content_hash}", headers=headers)
            if r.status_code == 404:
                print("[+] Alice successfully stopped serving purged payload.")
            else:
                raise Exception(f"Alice still serving purged payload! (status {r.status_code})")

    finally:
        print("[*] Cleaning up nodes...")
        alice.stop()
        bob.stop()

if __name__ == "__main__":
    import sys
    asyncio.run(run_e2e())

import pytest

@pytest.mark.asyncio
async def test_e2e_sync_v2():
    await run_e2e()
