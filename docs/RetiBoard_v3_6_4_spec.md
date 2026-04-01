# Project Specification: RetiBoard (v3.6.4)

Sovereign, Scalable Decentralized Imageboard over Reticulum

Updated for Chunked Multi-Peer Payload Transfer, Resume-Safe Fetching, Attachment Fetch Controls, and P2P Board Discovery over LXMF (v3.6.2 -> v3.6.3 -> v3.6.4).

This document is the unified, standalone v3.6.4 specification. It completely supersedes all previous versions (v3.6.3, v3.6.2).

---

## 1. Core Vision & Philosophy

RetiBoard is a sovereign, censorship-resistant imageboard built on the Reticulum Network Stack (RNS). It recreates the classic anonymous imageboard model while operating without any centralized storage or infrastructure.

The design is explicitly oriented toward short-lived, ephemeral content — shitposting, not archiving. Data is aggressively pruned by default. Nothing is guaranteed to persist beyond its natural lifetime.

**Principles:**
- **Classic Imageboard Behavior** — Catalog view, thread bumping, OP-first layout, anonymous posting, chronological + bump ordering.
- **Strict Infrastructure Neutrality** — Reticulum routing nodes (`rnsd`) only route packets. No persistent storage, indexing, or application logic at the infrastructure level.
- **User-Local Persistence** — All storage lives exclusively on end-user machines. Each client is its own node and cache.
- **Aggressive Ephemeral Defaults & Thread Lifecycle** — Active threads receive automatic TTL extensions; abandoned threads are purged entirely (no stubs).
- **End-to-End Content Opacity** — Payloads are fully encrypted. The local backend never holds decryption keys or inspects content.
- **Client-Side Sovereignty** — Moderation, filtering, fetch policy, and retention are 100% local decisions.

---

## 2. Network & Node Model

### 2.1 Reticulum Routing Nodes (`rnsd`)

Pure packet routing only. Short-lived in-memory buffering permitted; no disk, no indexing, no awareness of RetiBoard.

### 2.2 User Nodes (RetiBoard Clients)

Each client is a full sovereign node.

RetiBoard Client:
- RNS Interface
- Python Backend (FastAPI + RNS)
  - `meta.db` (SQLite index)
  - `/payloads/` (opaque encrypted blobs)
  - Gossip Sync Engine (thread-centric)
- Vue SPA (served locally at `http://127.0.0.1:8787`)

### 2.3 Dual-Destination Model (Mandatory)

Each node operates with two distinct RNS destinations that serve fundamentally different roles:

- **Board Destination (`board_id`)** — Used exclusively for broadcast metadata, gossip (HAVE announcements), and board-level discovery. It is NOT a valid LXMF delivery endpoint and MUST never be used as an LXMF target.
- **Peer LXMF Delivery Destination (`peer_lxmf_hash`)** — Derived from the node's LXMF identity (`app="lxmf"`, `aspect="delivery"`). It is the sole valid target for all point-to-point communication: `DELTA_REQUEST`, `DELTA_RESPONSE`, `PAYLOAD` transfer, and all Chunk-control/Board-list messages.

**Prohibited Pattern:** Sending LXMF to `board_id` is explicitly prohibited under all circumstances. This includes fallback scenarios. Board destinations are structurally incapable of receiving LXMF.

### 2.4 Relay Mode (`--relay`)

Relay-mode nodes apply exactly the same storage and pruning rules as regular clients: same TTL, same thread-lifecycle logic, same background prune job every 15 minutes, same `is_abandoned` purging. They participate in HAVE announcements and delta gossip exactly like normal nodes. They also serve and request chunks. The only difference is the absence of the Vue SPA and local UI.

---

## 3. Data Model

### 3.1 Structural Metadata (Plaintext)

Transmitted and stored in plaintext for indexing and sync. Strictly structural — never leaks content.
Prohibited fields: no text previews, no image hints, no subjects, no filenames.

```json
{
  "post_id": "",
  "thread_id": "",
  "parent_id": "",
  "timestamp": 0,
  "bump_flag": false,
  "content_hash": "",
  "payload_size": 0,
  "attachment_content_hash": "",
  "attachment_payload_size": 0,
  "has_attachments": false,
  "text_only": false,
  "identity_hash": "",
  "pow_nonce": "",
  "thread_last_activity": 0,
  "is_abandoned": false
}
```

### 3.2 Encrypted Payloads — Split-Blob Model

The split-blob model is normative:
- Text and attachments are encrypted independently with the board AES-GCM key.
- Each blob has its own nonce.
- `content_hash` is `SHA-256(encrypted_text_blob)`
- `attachment_content_hash` is `SHA-256(encrypted_attachment_blob)`

The backend stores opaque `.bin` files only. Media types are declared only inside the encrypted attachment blob.

### 3.3 Chunked Transfer Model

Chunking is a **transport optimization only**. It does **not** change canonical payload identity.
For large payload transfers, the canonical encrypted blob may be represented transiently by:
- one **ChunkManifest**
- many fixed-size **encrypted chunks**

The canonical identity remains:
`blob_hash = SHA-256(full_encrypted_blob)`

Chunking rules:
1. Split occurs **after encryption**, never before.
2. Chunk boundaries are deterministic for a given encrypted blob and chunk size.
3. Each chunk has its own `chunk_hash = SHA-256(chunk_bytes)`.
4. The final stored object remains the original canonical encrypted blob under `/payloads/<blob_hash>.bin`.

### 3.4 Chunk Manifest

A manifest describes one canonical encrypted blob. It is structural-only metadata and contains no plaintext content.

```json
{
  "manifest_version": 1,
  "board_id": "",
  "blob_hash": "",
  "blob_kind": "text|attachments",
  "blob_size": 0,
  "chunk_size": 0,
  "chunk_count": 0,
  "merkle_root": null,
  "entries": [
    {
      "chunk_index": 0,
      "offset": 0,
      "size": 0,
      "chunk_hash": ""
    }
  ]
}
```

### 3.5 Chunk Availability Summary

Peers may advertise structural chunk availability for a blob:

```json
{
  "board_id": "",
  "blob_hash": "",
  "chunk_count": 0,
  "complete": false,
  "ranges": [[0, 15], [18, 23]]
}
```

Availability advertisements are advisory and peer-scoped.

### 3.6 Board Announce Schema (v2)

Signed RNS/LXMF announce. Shared as `rns://board/<board_id>`.

```json
{
  "board_id": "",
  "display_name": "",
  "text_only": false,
  "default_ttl_seconds": 172800,
  "bump_decay_rate": 3600,
  "max_active_threads_local": 50,
  "pow_difficulty": 0,
  "key_material": "",
  "announce_version": 2,
  "peer_lxmf_hash": ""
}
```
`peer_lxmf_hash` is mandatory. `announce_version` must be 2.

---

## 4. Local Storage & Pruning

Disk layout:
```text
/boards/<board_id>/
  meta.db
  /payloads/<content_hash>.bin
  /payloads/<attachment_content_hash>.bin
  /chunk_cache/<blob_hash>/<chunk_index>.part
  /chunk_cache/<blob_hash>/assembly.tmp
```

`/chunk_cache/` is ephemeral staging only. It MUST be pruned with the parent blob/thread lifecycle.

Additional structural tables in `meta.db` include:
`chunk_manifests`, `chunk_manifest_entries`, `peer_chunk_availability`, `chunk_fetch_sessions`, `chunk_request_states`, `chunk_peer_penalties`.

**Pruning rules:**
- Retention is enforced at thread granularity.
- A new thread starts with `default_ttl_seconds`.
- Each bump refills `bump_decay_rate`, capped so the thread never has more than `default_ttl_seconds` remaining.
- Threads with `expiry_timestamp <= now` become expired/abandoned (`is_abandoned=true`).
- Abandoned threads are fully deleted: metadata, payloads, chunk cache, manifests, and active chunk sessions.

---

## 5. Payload Encryption

Board-wide AES-GCM key derived from public `key_material` via HKDF in the frontend only. Nonce is prefixed to each `.bin` file.

**Key Distribution & Threat Model**
The `key_material` field in the signed board announce is deliberately public. Anyone who obtains the announce packet can derive the AES-GCM board key. Board access is controlled by announce distribution only (obscurity via link sharing for public boards, invite-only for semi-private boards). The backend never receives `key_material` or the derived key.

---

## 6. Data Flow

### 6.1 Post Creation

1. Write post text and optional attachments.
2. Construct payloads.
3. Encrypt text and attachments independently.
4. Hash ciphertext independently (`content_hash`, `attachment_content_hash`).
5. Construct metadata record.
6. Solve PoW.
7. Broadcast metadata via LXMF to peer LXMF destinations.
8. Transfer text and attachment payloads via RNS Resource or Chunks.

### 6.2 Reception

On receiving metadata: validate PoW, validate flags, store in `meta.db`, update thread state.
On receiving a whole payload blob: verify hash, store as `.bin`.

### 6.3 Chunked Payload Reception

For chunked transfers:
1. Receive/generate a manifest.
2. Request chunks from peers.
3. **Pre-validate** each received chunk: valid session, valid index, size matches, assigned peer matches, `SHA-256(chunk_bytes)` matches manifest entry.
4. Write verified chunk directly to `assembly.tmp`.
5. When fully assembled, stream-hash the complete file.
6. Atomically commit the canonical `/payloads/<blob_hash>.bin`.
If whole-blob verification fails, the payload MUST NOT be committed.

### 6.4 Crash-Safe Restore

When restoring a partial chunk session after restart, each stored chunk region in `assembly.tmp` SHOULD be verified against the manifest hash before being marked present. Corrupted/missing regions are downgraded to missing and re-requested.

---

## 7. Synchronization Model (Thread-Centric)

Core principle: threads remain the atomic unit of sync and pruning. Abandoned threads are never gossiped.

### 7.1 Three-Tier Strategy

#### Tier 1 — LXMF Broadcast
New metadata is sent via LXMF to known peers' LXMF delivery destinations.

#### Tier 2 — HAVE Announcements
Peers periodically advertise only active threads via lightweight HAVE packets.
- Cap: 20 most recently active threads (10 on LoRa links).

#### Tier 3 — Delta Gossip + Payload Fetch

**Delta Sync (Metadata)**
`DELTA_REQUEST` and `DELTA_RESPONSE` over LXMF sync missing metadata.

**Payload Fetch**
Always on-demand. Nodes MUST NOT proactively fetch payloads during gossip.

*Whole-Blob Fetch:*
`PAYLOAD_REQUEST` / `PAYLOAD_RESPONSE` (binary over RNS Resource transfer).

*Chunked Fetch:*
Control-plane messages over LXMF:
- `CHUNK_MANIFEST_REQUEST` / `CHUNK_MANIFEST_RESPONSE` / `CHUNK_MANIFEST_UNAVAILABLE`
- `CHUNK_OFFER` / `CHUNK_REQUEST` / `CHUNK_CANCEL`

Chunk bytes are delivered over the payload RNS Resource data plane, not over LXMF.
- Requester may fetch different chunks from multiple peers concurrently.
- `abandoned` and `policy_rejected` are hard-stop reasons. `withheld_local_policy` is a peer-scoped negative.
- Once one valid duplicate chunk arrives, the requester SHOULD send `CHUNK_CANCEL` to sibling peers where possible.

### 7.2 Rate Limiting & Backpressure

- Max 5 concurrent thread syncs per board (2 on LoRa).
- Exponential backoff + jitter on failures.
- Implementations SHOULD cap chunk concurrency per peer and cool down peers that repeatedly time out or serve invalid chunks.
- Duplicate endgame requests MUST be bounded.

### 7.3 Opportunistic Replication

When a client receives new content for an active thread, it forwards the metadata to 1–3 recently seen peers. Payloads may be served opportunistically upon request, but nodes remain content-blind and never archive beyond normal TTL.

---

## 8. Node Identity, LXMF Routing & Peer Discovery (v3.6.4 Updates)

### 8.1 Primary LXMF Identity

Each node MUST maintain a primary LXMF identity used for sending and receiving all point-to-point LXMF messages.

### 8.2 Identity Announcement (MTU Safe)

Each node MUST periodically announce its LXMF identity. To respect RNS MTU limits (~384 bytes usable), the Identity Announce MUST be lightweight and MUST NOT contain unbounded arrays like lists of subscribed boards.

**App Data Payload:**
```json
{
  "app": "retiboard",
  "version": "3.6.4"
}
```

### 8.3 P2P Board Discovery Handshake

Because the Identity Announce no longer contains board lists, nodes discover shared boards dynamically via P2P LXMF messaging immediately after learning of a peer.

1. **Identity Received:** Node A receives Node B's Identity Announce.
2. **List Request:** Node A sends a `BOARD_LIST_REQ` (LXMF control message) to Node B's `peer_lxmf_hash`.
3. **List Response:** Node B gathers its local list of subscribed boards and responds with a `BOARD_LIST_RES` containing a JSON array of `board_id`s. Since LXMF automatically handles packetization (Links/Resources), this payload can safely exceed the MTU limit.
4. **Catch-up Sync:** Node A processes the array, registers the peer for shared boards, and immediately schedules catch-up sync.
5. **Cold-Start Push:** If Node A notices Node B is missing boards that Node A owns or is subscribed to, Node A may proactively push those `BOARD_ANNOUNCE` payloads to Node B via LXMF so Node B can discover and subscribe to them.

### 8.4 Trust Model

- `peer_lxmf_hash` extracted from a board announce is a hint (advisory).
- `message.source` from a received LXMF message is authoritative. It confirms both identity and reachability.

---

## 9. Path State Machine

States: `unknown` -> `requested` -> `known` -> `stale` -> `unreachable`
Includes configurable timers, exponential backoff with jitter, and link-type awareness.

---

## 10. Message Queue

Each node MUST implement a per-peer message queue (keyed by `peer_lxmf_hash`).

**Priority tiers:**
1. **Control** — `DELTA_REQUEST`, `DELTA_RESPONSE`, `CHUNK_MANIFEST_REQUEST`, `CHUNK_MANIFEST_RESPONSE`, `CHUNK_MANIFEST_UNAVAILABLE`, `CHUNK_OFFER`, `CHUNK_REQUEST`, `CHUNK_CANCEL`, `BOARD_LIST_REQ`, `BOARD_LIST_RES`.
2. **Data** — Whole-blob payload requests.

Control messages always dequeue before data messages. Flushed in priority order on path discovery. Persisted chunk session state is separate from the peer queue and is recommended for restart-safe resume.

---

## 11. Low-Bandwidth & Text-Only Mode

**Text-Only Boards (`text_only: true`):**
- Attachment payloads are never broadcast or requested.
- Chunk manifests/offers/requests for attachment blobs MUST be rejected.
- Unexpected attachments chunks MUST be discarded silently.

**LoRa Client Behavior:**
Clients on low-bandwidth links SHOULD default to text-only boards, skip attachment payload requests, reduce gossip frequency (30–60 min interval), use a 10-thread HAVE cap, and disable chunk swarming for attachments.

**PoW:** Low-power devices may enforce a local maximum difficulty cap.

---

## 12. Frontend State Model

Text remains loaded immediately on post render. Attachments remain loaded on demand.

- Frontend may show structural attachment-fetch progress derived from local chunk session state.
- Attachment fetch UI may support **pause**, **resume**, and **cancel** controls.
- Paused/resumed state is local and does not alter canonical payload identity.
- The frontend remains responsible for all decryption. The backend remains content-blind.

---

## 13. Anti-Spam & Identity Model

- **PoW:** Difficulty is per-board. `hash(metadata + nonce) < difficulty_target`. Applies uniformly to OP and replies.
- **Identity:** Reticulum native identities. Identity hash included in plaintext metadata for client-side filtering and blocking.

---

## 14. Moderation Model

Client-Side Only. Identity blocking, thread hiding, content filtering. All moderation state is local. Shared blocklists (signed by issuer) can be distributed via RNS/LXMF.

---

## 15. Compliance Rules (Normative)

A compliant RetiBoard node MUST:
1. Maintain a primary LXMF identity distinct from any board destination.
2. Announce its LXMF identity periodically with a lightweight (MTU-safe) payload.
3. Discover peer board memberships via P2P `BOARD_LIST_REQ`/`RES` over LXMF.
4. Include `peer_lxmf_hash` and `announce_version: 2` in all board announces.
5. Never send LXMF to a board destination under any circumstance.
6. Preserve canonical payload identity as `SHA-256(encrypted_blob)` for text and attachment blobs.
7. Split chunks only after encryption.
8. Verify each received chunk against the manifest hash before reassembly admission.
9. Verify the final assembled encrypted blob against the canonical blob hash before commit.
10. Keep chunk-control messages and board list exchange on peer LXMF delivery destinations only.
11. Keep payload bytes on the RNS Resource data plane only.
12. Enforce board/thread `text_only` policy for whole-blob and chunked attachment transfer alike.
13. Prune chunk cache, manifests, and chunk session state with the parent thread/blob lifecycle.
14. Treat `peer_lxmf_hash` from announces as advisory until validated by LXMF `message.source`.
15. Support legacy peers via broadcast-only fallback where applicable.
16. Preserve the content-blind backend model under all chunk-transfer modes.
