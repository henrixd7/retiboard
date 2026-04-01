/**
 * Pinia moderation store.
 *
 * Manages all local structural content-control state, synced against the
 * backend content_control table via REST on board open.
 *
 * Purge lifecycle
 * ───────────────
 * When the user purges a post or thread, the backend deletes the metadata
 * rows and payload files, then writes a deny tombstone.  The frontend must
 * keep the post/thread object in memory so it can render the "purged"
 * placeholder — it cannot re-fetch from the API because the row is gone.
 *
 * We do this by saving "stubs" at purge time:
 *   - purgedPostStubs  Map<post_id,  PostStub>
 *   - purgedThreadStubs Map<thread_id, ThreadStub>
 *
 * A stub is the minimal shape each view needs to render its placeholder.
 * Stubs are session-local (cleared on board change / hydrate) — they
 * survive reactive re-renders within a session but not a page reload,
 * which is fine: after a reload the tombstone is still in the DB so the
 * admission filter prevents re-admission, and the placeholder will not
 * appear (the post simply won't be in the thread at all).
 *
 * Unpurge lifecycle
 * ─────────────────
 * Unpurge calls DELETE /control/purge-post/{id} or /purge-thread/{id},
 * which clears the tombstone row.  The metadata is NOT restored by this
 * call — content re-propagates from the network via gossip.  The stub is
 * removed from the store and the placeholder transitions to an
 * "awaiting network" state in the view.
 *
 * Session exceptions (keyword / one-off "show anyway")
 * ─────────────────────────────────────────────────────
 * allowedThreadExceptions and allowedPostExceptions are purely in-memory
 * and never persisted.  They let the user bypass a hide/block rule for
 * the current session without changing the underlying control record.
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import {
  getPostBucketFromState,
  getPostHiddenReasonFromState,
  getPostReasonFromState,
  getThreadBucketFromState,
  getThreadHiddenReasonFromState,
  getThreadReasonFromState,
  shouldHidePostFromState,
  shouldHideThreadFromState,
} from '../moderation/structural.js'
import { apiJson } from '../utils/api.js'

export const useModerationStore = defineStore('moderation', () => {
  const GLOBAL_BANNED_FILE_HASHES_KEY = 'retiboard:global-banned-file-hashes'
  const GLOBAL_HIDDEN_BANNED_PLACEHOLDERS_KEY = 'retiboard:global-hidden-banned-file-placeholders'

  const activeBoardId = ref(null)
  const loading = ref(false)
  const error = ref(null)

  // ── Persisted sets (hydrated from backend on board open) ────────────────
  const blockedIdentities = ref(new Set())
  const hiddenIdentities  = ref(new Set())
  const hiddenThreads     = ref(new Set())
  const hiddenPosts       = ref(new Set())
  const purgedThreads     = ref(new Set())
  const purgedPosts       = ref(new Set())
  const bannedAttachments = ref(new Set())
  const bannedFileHashes = ref(loadPersistedGlobalSet(GLOBAL_BANNED_FILE_HASHES_KEY))
  const hiddenBannedFilePlaceholders = ref(loadPersistedGlobalSet(GLOBAL_HIDDEN_BANNED_PLACEHOLDERS_KEY))

  // ── Session-local stubs ────────────────────────────────────────────────
  // Saved at purge time. Shape must match what views need to render the
  // placeholder: { post_id, thread_id, identity_hash } for posts;
  // { thread_id, identity_hash, thread_last_activity } for threads.
  const purgedPostStubs   = ref(new Map())   // post_id → stub object
  const purgedThreadStubs = ref(new Map())   // thread_id → stub object

  // ── Session-local escape hatches ───────────────────────────────────────
  const allowedThreadExceptions = ref(new Set())
  const allowedPostExceptions   = ref(new Set())

  const hydrated = computed(() => !!activeBoardId.value && !loading.value)
  const bannedFileHashList = computed(() => Array.from(bannedFileHashes.value).sort())

  function storageKey(boardId) {
    return boardId ? `retiboard:moderation-stubs:${boardId}` : null
  }

  function storageArea() {
    if (typeof window !== 'undefined' && window.localStorage) return window.localStorage
    if (typeof globalThis !== 'undefined' && globalThis.localStorage) return globalThis.localStorage
    return null
  }

  function loadPersistedGlobalSet(key) {
    const storage = storageArea()
    if (!storage) return new Set()
    try {
      const raw = storage.getItem(key)
      const parsed = raw ? JSON.parse(raw) : []
      return new Set(Array.isArray(parsed) ? parsed.filter(Boolean) : [])
    } catch {
      return new Set()
    }
  }

  function persistGlobalSet(key, setRef) {
    const storage = storageArea()
    if (!storage) return
    try {
      storage.setItem(key, JSON.stringify(Array.from(setRef.value)))
    } catch {}
  }

  function persistStubs(boardId = activeBoardId.value) {
    const key = storageKey(boardId)
    if (!key || typeof window === 'undefined') return
    const payload = {
      posts: Array.from(purgedPostStubs.value.values()),
      threads: Array.from(purgedThreadStubs.value.values()),
    }
    try {
      window.localStorage.setItem(key, JSON.stringify(payload))
    } catch {}
  }

  function loadPersistedStubs(boardId) {
    purgedPostStubs.value = new Map()
    purgedThreadStubs.value = new Map()
    const key = storageKey(boardId)
    if (!key || typeof window === 'undefined') return
    try {
      const raw = window.localStorage.getItem(key)
      if (!raw) return
      const parsed = JSON.parse(raw) || {}
      const postMap = new Map()
      for (const stub of Array.isArray(parsed.posts) ? parsed.posts : []) {
        if (stub?.post_id) postMap.set(stub.post_id, stub)
      }
      const threadMap = new Map()
      for (const stub of Array.isArray(parsed.threads) ? parsed.threads : []) {
        if (stub?.thread_id) threadMap.set(stub.thread_id, stub)
      }
      purgedPostStubs.value = postMap
      purgedThreadStubs.value = threadMap
    } catch {}
  }

  // ── State reset ────────────────────────────────────────────────────────

  function resetState() {
    blockedIdentities.value = new Set()
    hiddenIdentities.value  = new Set()
    hiddenThreads.value     = new Set()
    hiddenPosts.value       = new Set()
    purgedThreads.value     = new Set()
    purgedPosts.value       = new Set()
    bannedAttachments.value = new Set()
    purgedPostStubs.value   = new Map()
    purgedThreadStubs.value = new Map()
    clearSessionExceptions()
  }

  function clearSessionExceptions() {
    allowedThreadExceptions.value = new Set()
    allowedPostExceptions.value   = new Set()
  }

  // ── Hydration ──────────────────────────────────────────────────────────

  async function hydrate(boardId) {
    if (!boardId) { activeBoardId.value = null; resetState(); return }
    loading.value = true
    error.value = null
    try {
      loadPersistedStubs(boardId)
      const data = await apiJson(`/api/boards/${boardId}/control/state`)
      activeBoardId.value     = boardId
      blockedIdentities.value = new Set(data.blocked_identities || [])
      hiddenIdentities.value  = new Set(data.hidden_identities || [])
      hiddenThreads.value     = new Set(data.hidden_threads || [])
      hiddenPosts.value       = new Set(data.hidden_posts || [])
      purgedThreads.value     = new Set(data.purged_threads || [])
      purgedPosts.value       = new Set(data.purged_posts || [])
      bannedAttachments.value = new Set(data.banned_attachments || [])
      clearSessionExceptions()
    } catch (e) {
      error.value = e.message
      activeBoardId.value = boardId
      resetState()
    } finally {
      loading.value = false
    }
  }

  function ensureBoard(boardId) {
    if (!boardId) throw new Error('boardId required')
    if (activeBoardId.value !== boardId) { activeBoardId.value = boardId; resetState(); loadPersistedStubs(boardId) }
  }

  // ── Session exceptions ─────────────────────────────────────────────────

  function allowThreadThisSession(threadId) {
    if (threadId) allowedThreadExceptions.value.add(threadId)
  }
  function allowPostThisSession(postId) {
    if (postId) allowedPostExceptions.value.add(postId)
  }

  // ── Query helpers ──────────────────────────────────────────────────────

  function isIdentityBlocked(identityHash) {
    return !!identityHash && blockedIdentities.value.has(identityHash)
  }
  function isIdentityHidden(identityHash) {
    return !!identityHash && hiddenIdentities.value.has(identityHash)
  }
  function isAttachmentBanned(attachmentContentHash) {
    return !!attachmentContentHash && bannedAttachments.value.has(attachmentContentHash)
  }
  function isFileHashBanned(fileHash) {
    return !!fileHash && bannedFileHashes.value.has(fileHash)
  }
  function isBannedFilePlaceholderHidden(fileHash) {
    return !!fileHash && hiddenBannedFilePlaceholders.value.has(fileHash)
  }
  function isThreadHidden(threadId) {
    return !!threadId && hiddenThreads.value.has(threadId)
  }
  function isPostHidden(postId) {
    return !!postId && hiddenPosts.value.has(postId)
  }
  function isThreadPurged(threadId) {
    return !!threadId && purgedThreads.value.has(threadId)
  }
  function isPostPurged(postId) {
    return !!postId && purgedPosts.value.has(postId)
  }
  function getPurgedPostStub(postId) {
    return purgedPostStubs.value.get(postId) || null
  }
  function getPurgedThreadStub(threadId) {
    return purgedThreadStubs.value.get(threadId) || null
  }

  // ── Structural decision helpers ────────────────────────────────────────

  function structuralStateSnapshot() {
    return {
      blockedIdentities:      blockedIdentities.value,
      hiddenIdentities:       hiddenIdentities.value,
      hiddenThreads:          hiddenThreads.value,
      hiddenPosts:            hiddenPosts.value,
      purgedThreads:          purgedThreads.value,
      purgedPosts:            purgedPosts.value,
      bannedAttachments:      bannedAttachments.value,
      allowedThreadExceptions:allowedThreadExceptions.value,
      allowedPostExceptions:  allowedPostExceptions.value,
    }
  }
  function getThreadReason(thread) {
    return getThreadReasonFromState(structuralStateSnapshot(), thread)
  }
  function getThreadHiddenReason(thread) {
    return getThreadHiddenReasonFromState(structuralStateSnapshot(), thread)
  }
  function getThreadBucket(thread) {
    return getThreadBucketFromState(structuralStateSnapshot(), thread)
  }
  function getPostReason(post) {
    return getPostReasonFromState(structuralStateSnapshot(), post)
  }
  function getPostHiddenReason(post) {
    return getPostHiddenReasonFromState(structuralStateSnapshot(), post)
  }
  function getPostBucket(post) {
    return getPostBucketFromState(structuralStateSnapshot(), post)
  }
  function shouldHideThread(thread) {
    return shouldHideThreadFromState(structuralStateSnapshot(), thread)
  }
  function shouldHidePostStructurally(post) {
    return shouldHidePostFromState(structuralStateSnapshot(), post)
  }

  // ── Hide / unhide thread ───────────────────────────────────────────────

  async function hideThread(boardId, threadId) {
    ensureBoard(boardId)
    const had = hiddenThreads.value.has(threadId)
    hiddenThreads.value.add(threadId)
    try {
      await apiJson(`/api/boards/${boardId}/control/hide-thread`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ thread_id: threadId }),
      })
    } catch (e) {
      if (!had) hiddenThreads.value.delete(threadId)
      throw e
    }
  }

  async function unhideThread(boardId, threadId) {
    ensureBoard(boardId)
    const had = hiddenThreads.value.has(threadId)
    hiddenThreads.value.delete(threadId)
    try {
      await apiJson(`/api/boards/${boardId}/control/hide-thread/${encodeURIComponent(threadId)}`, {
        method: 'DELETE',
      })
    } catch (e) {
      if (had) hiddenThreads.value.add(threadId)
      throw e
    }
  }

  // ── Hide / unhide post ─────────────────────────────────────────────────

  async function hidePost(boardId, postId) {
    ensureBoard(boardId)
    const had = hiddenPosts.value.has(postId)
    hiddenPosts.value.add(postId)
    try {
      await apiJson(`/api/boards/${boardId}/control/hide-post`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ post_id: postId }),
      })
    } catch (e) {
      if (!had) hiddenPosts.value.delete(postId)
      throw e
    }
  }

  async function unhidePost(boardId, postId) {
    ensureBoard(boardId)
    const had = hiddenPosts.value.has(postId)
    hiddenPosts.value.delete(postId)
    try {
      await apiJson(`/api/boards/${boardId}/control/hide-post/${encodeURIComponent(postId)}`, {
        method: 'DELETE',
      })
    } catch (e) {
      if (had) hiddenPosts.value.add(postId)
      throw e
    }
  }

  // ── Purge post ─────────────────────────────────────────────────────────

  /**
   * Purge a post. Saves a minimal stub before calling the API so the view
   * can render a placeholder even after the metadata row is deleted.
   *
   * stub shape: { post_id, thread_id, identity_hash }
   */
  async function purgePost(boardId, postId, stub = null) {
    ensureBoard(boardId)
    const wasPurged = purgedPosts.value.has(postId)
    const priorStub = purgedPostStubs.value.get(postId)
    purgedPosts.value.add(postId)
    if (stub) { purgedPostStubs.value.set(postId, stub); persistStubs(boardId) }
    try {
      await apiJson(`/api/boards/${boardId}/control/purge-post`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ post_id: postId }),
      })
    } catch (e) {
      if (!wasPurged) purgedPosts.value.delete(postId)
      if (stub) {
        if (priorStub) purgedPostStubs.value.set(postId, priorStub)
        else purgedPostStubs.value.delete(postId)
        persistStubs(boardId)
      }
      throw e
    }
  }

  /**
   * Lift a post purge tombstone.
   *
   * Removes the deny rule so gossip can re-admit the post. The post
   * metadata was deleted — re-population happens via network sync.
   * The stub is kept until the view explicitly drops it (after a
   * successful re-fetch confirms the post is back in the API).
   *
   * After clearing the tombstone we fire a best-effort HAVE_REQ to
   * known peers (§7.1 Tier 2) so the sync engine immediately tries
   * to re-acquire the missing content rather than waiting up to
   * 15 min for the next periodic gossip cycle.
   */
  async function unpurgePost(boardId, postId) {
    ensureBoard(boardId)
    const had = purgedPosts.value.has(postId)
    purgedPosts.value.delete(postId)
    try {
      await apiJson(
        `/api/boards/${boardId}/control/purge-post/${encodeURIComponent(postId)}`,
        { method: 'DELETE' },
      )
      // Fire-and-forget: ask peers for a fresh HAVE so we re-acquire the
      // deleted content as soon as possible.  Failures are silent — the
      // node will catch up on the next periodic cycle regardless.
      apiJson(`/api/boards/${boardId}/control/request-catchup`, { method: 'POST' }).catch(() => {})
    } catch (e) {
      if (had) purgedPosts.value.add(postId)
      throw e
    }
    persistStubs(boardId)
  }

  /** Remove a post stub from memory (called by the view once gossip repopulates). */
  function dropPurgedPostStub(postId) {
    purgedPostStubs.value.delete(postId)
    persistStubs()
  }

  // ── Purge thread ───────────────────────────────────────────────────────

  /**
   * Purge a thread. Saves a minimal stub before calling the API.
   *
   * stub shape: { thread_id, identity_hash, thread_last_activity }
   */
  async function purgeThread(boardId, threadId, stub = null) {
    ensureBoard(boardId)
    const wasPurged = purgedThreads.value.has(threadId)
    const priorStub = purgedThreadStubs.value.get(threadId)
    purgedThreads.value.add(threadId)
    if (stub) { purgedThreadStubs.value.set(threadId, stub); persistStubs(boardId) }
    try {
      await apiJson(`/api/boards/${boardId}/control/purge-thread`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ thread_id: threadId }),
      })
    } catch (e) {
      if (!wasPurged) purgedThreads.value.delete(threadId)
      if (stub) {
        if (priorStub) purgedThreadStubs.value.set(threadId, priorStub)
        else purgedThreadStubs.value.delete(threadId)
        persistStubs(boardId)
      }
      throw e
    }
  }

  /**
   * Lift a thread purge tombstone.
   *
   * Removes the deny rule so gossip can re-admit thread posts. Content
   * re-populates from the network. The stub is kept until the view removes it.
   *
   * After clearing the tombstone we fire a best-effort HAVE_REQ to known
   * peers (§7.1 Tier 2) so the sync engine re-acquires the thread without
   * waiting for the next periodic gossip cycle.
   */
  async function unpurgeThread(boardId, threadId) {
    ensureBoard(boardId)
    const had = purgedThreads.value.has(threadId)
    purgedThreads.value.delete(threadId)
    try {
      await apiJson(
        `/api/boards/${boardId}/control/purge-thread/${encodeURIComponent(threadId)}`,
        { method: 'DELETE' },
      )
      // Fire-and-forget: stimulate immediate gossip catch-up.
      apiJson(`/api/boards/${boardId}/control/request-catchup`, { method: 'POST' }).catch(() => {})
    } catch (e) {
      if (had) purgedThreads.value.add(threadId)
      throw e
    }
    persistStubs(boardId)
  }

  /** Remove a thread stub from memory. */
  function dropPurgedThreadStub(threadId) {
    purgedThreadStubs.value.delete(threadId)
    persistStubs()
  }

  // ── Hide / unhide identity ─────────────────────────────────────────────

  async function hideIdentity(boardId, identityHash) {
    ensureBoard(boardId)
    const had = hiddenIdentities.value.has(identityHash)
    hiddenIdentities.value.add(identityHash)
    try {
      await apiJson(`/api/boards/${boardId}/control/hide-identity`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ identity_hash: identityHash }),
      })
    } catch (e) {
      if (!had) hiddenIdentities.value.delete(identityHash)
      throw e
    }
  }

  async function unhideIdentity(boardId, identityHash) {
    ensureBoard(boardId)
    const had = hiddenIdentities.value.has(identityHash)
    hiddenIdentities.value.delete(identityHash)
    try {
      await apiJson(`/api/boards/${boardId}/control/hide-identity/${encodeURIComponent(identityHash)}`, {
        method: 'DELETE',
      })
    } catch (e) {
      if (had) hiddenIdentities.value.add(identityHash)
      throw e
    }
  }

  // ── Ban / unban identity ───────────────────────────────────────────────

  async function banIdentity(boardId, identityHash) {
    ensureBoard(boardId)
    blockedIdentities.value.add(identityHash)
    hiddenIdentities.value.delete(identityHash) // ban supersedes hide
    try {
      return await apiJson(`/api/boards/${boardId}/control/ban-identity`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ identity_hash: identityHash }),
      })
    } catch (e) {
      blockedIdentities.value.delete(identityHash)
      throw e
    }
  }

  async function unbanIdentity(boardId, identityHash) {
    ensureBoard(boardId)
    const had = blockedIdentities.value.has(identityHash)
    blockedIdentities.value.delete(identityHash)
    try {
      await apiJson(`/api/boards/${boardId}/control/ban-identity/${encodeURIComponent(identityHash)}`, {
        method: 'DELETE',
      })
      // Fire-and-forget: stimulate immediate gossip catch-up.
      apiJson(`/api/boards/${boardId}/control/request-catchup`, { method: 'POST' }).catch(() => {})
    } catch (e) {
      if (had) blockedIdentities.value.add(identityHash)
      throw e
    }
  }

  // ── Ban / unban attachment ─────────────────────────────────────────────

  async function banAttachment(boardId, attachmentContentHash) {
    ensureBoard(boardId)
    const had = bannedAttachments.value.has(attachmentContentHash)
    bannedAttachments.value.add(attachmentContentHash)
    try {
      await apiJson(`/api/boards/${boardId}/control/ban-attachment`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ attachment_content_hash: attachmentContentHash }),
      })
    } catch (e) {
      if (!had) bannedAttachments.value.delete(attachmentContentHash)
      throw e
    }
  }

  async function unbanAttachment(boardId, attachmentContentHash) {
    ensureBoard(boardId)
    const had = bannedAttachments.value.has(attachmentContentHash)
    bannedAttachments.value.delete(attachmentContentHash)
    try {
      await apiJson(`/api/boards/${boardId}/control/ban-attachment/${encodeURIComponent(attachmentContentHash)}`, {
        method: 'DELETE',
      })
    } catch (e) {
      if (had) bannedAttachments.value.add(attachmentContentHash)
      throw e
    }
  }

  function banFileHash(fileHash) {
    const normalized = String(fileHash || '').trim().toLowerCase()
    if (!normalized) return
    bannedFileHashes.value.add(normalized)
    hiddenBannedFilePlaceholders.value.delete(normalized)
    persistGlobalSet(GLOBAL_BANNED_FILE_HASHES_KEY, bannedFileHashes)
    persistGlobalSet(GLOBAL_HIDDEN_BANNED_PLACEHOLDERS_KEY, hiddenBannedFilePlaceholders)
  }

  function unbanFileHash(fileHash) {
    const normalized = String(fileHash || '').trim().toLowerCase()
    if (!normalized) return
    bannedFileHashes.value.delete(normalized)
    hiddenBannedFilePlaceholders.value.delete(normalized)
    persistGlobalSet(GLOBAL_BANNED_FILE_HASHES_KEY, bannedFileHashes)
    persistGlobalSet(GLOBAL_HIDDEN_BANNED_PLACEHOLDERS_KEY, hiddenBannedFilePlaceholders)
  }

  function hideBannedFilePlaceholder(fileHash) {
    const normalized = String(fileHash || '').trim().toLowerCase()
    if (!normalized) return
    hiddenBannedFilePlaceholders.value.add(normalized)
    persistGlobalSet(GLOBAL_HIDDEN_BANNED_PLACEHOLDERS_KEY, hiddenBannedFilePlaceholders)
  }

  function showBannedFilePlaceholder(fileHash) {
    const normalized = String(fileHash || '').trim().toLowerCase()
    if (!normalized) return
    hiddenBannedFilePlaceholders.value.delete(normalized)
    persistGlobalSet(GLOBAL_HIDDEN_BANNED_PLACEHOLDERS_KEY, hiddenBannedFilePlaceholders)
  }

  return {
    activeBoardId,
    loading,
    error,
    hydrated,
    // Sets
    blockedIdentities,
    hiddenIdentities,
    hiddenThreads,
    hiddenPosts,
    purgedThreads,
    purgedPosts,
    bannedAttachments,
    bannedFileHashes,
    hiddenBannedFilePlaceholders,
    bannedFileHashList,
    purgedPostStubs,
    purgedThreadStubs,
    allowedThreadExceptions,
    allowedPostExceptions,
    // Lifecycle
    hydrate,
    resetState,
    clearSessionExceptions,
    // Session exceptions
    allowThreadThisSession,
    allowPostThisSession,
    // Query
    isIdentityBlocked,
    isIdentityHidden,
    isAttachmentBanned,
    isFileHashBanned,
    isBannedFilePlaceholderHidden,
    isThreadHidden,
    isPostHidden,
    isThreadPurged,
    isPostPurged,
    getPurgedPostStub,
    getPurgedThreadStub,
    // Decision helpers
    getThreadReason,
    getThreadHiddenReason,
    getThreadBucket,
    getPostReason,
    getPostHiddenReason,
    getPostBucket,
    shouldHideThread,
    shouldHidePostStructurally,
    // Actions
    hideThread,
    unhideThread,
    hidePost,
    unhidePost,
    purgePost,
    unpurgePost,
    dropPurgedPostStub,
    purgeThread,
    unpurgeThread,
    dropPurgedThreadStub,
    hideIdentity,
    unhideIdentity,
    banIdentity,
    unbanIdentity,
    banAttachment,
    unbanAttachment,
    banFileHash,
    unbanFileHash,
    hideBannedFilePlaceholder,
    showBannedFilePlaceholder,
  }
})
