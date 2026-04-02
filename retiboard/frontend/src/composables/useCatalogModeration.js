import { ref } from 'vue'

function getThreadModerationTarget(thread) {
  return {
    thread_id: thread.thread_id,
    identity_hash: thread?.identity_hash || thread?.op_identity_hash || '',
  }
}

/**
 * Owns catalog moderation actions and the local state eviction they require.
 */
export function useCatalogModeration(options) {
  const {
    boardId,
    settings,
    moderation,
    scheduleCatalogRefetch,
    getLiveThread,
    removeThread,
    ensureThreadContent,
    evictCatalogThreadState,
  } = options

  const moderationBusy = ref(false)

  function threadReason(thread) {
    if (thread._isStub) return 'purged'
    return moderation.getThreadReason(getThreadModerationTarget(thread))
  }

  function threadHiddenReason(thread) {
    return moderation.getThreadHiddenReason(getThreadModerationTarget(thread))
  }

  function isThreadAwaitingNetwork(thread) {
    return thread._isStub && !moderation.isThreadPurged(thread.thread_id)
  }

  function evictAndRemoveThread(threadId) {
    const liveThread = getLiveThread(threadId)
    evictCatalogThreadState(liveThread || threadId)
    removeThread(threadId)
  }

  async function restoreThread(thread) {
    const reason = threadHiddenReason(thread)
    moderationBusy.value = true
    try {
      if (reason === 'hidden_thread') {
        await moderation.unhideThread(boardId(), thread.thread_id)
      } else if (reason === 'hidden_post') {
        await moderation.unhidePost(boardId(), thread.thread_id)
      } else if (reason === 'hidden_identity') {
        const identityHash = thread.identity_hash || thread.op_identity_hash
        if (identityHash) await moderation.unhideIdentity(boardId(), identityHash)
      }

      ensureThreadContent(thread)
    } finally {
      moderationBusy.value = false
    }
  }

  async function hideThreadEntry(thread) {
    moderationBusy.value = true
    try {
      await moderation.hideThread(boardId(), thread.thread_id)
    } finally {
      moderationBusy.value = false
    }
  }

  function pinThreadEntry(thread) {
    settings.pinThread(boardId(), thread.thread_id)
  }

  function unpinThreadEntry(thread) {
    settings.unpinThread(boardId(), thread.thread_id)
  }

  async function purgeThreadEntry(thread) {
    if (settings.isThreadPinned(boardId(), thread.thread_id)) {
      window.alert('Unpin this thread before purging it.')
      return
    }

    moderationBusy.value = true
    try {
      const stub = {
        thread_id: thread.thread_id,
        identity_hash: thread.identity_hash || thread.op_identity_hash || '',
        thread_last_activity: thread.thread_last_activity || 0,
      }
      await moderation.purgeThread(boardId(), thread.thread_id, stub)
      evictAndRemoveThread(thread.thread_id)
    } finally {
      moderationBusy.value = false
    }
  }

  async function unpurgeThreadEntry(thread) {
    moderationBusy.value = true
    try {
      evictAndRemoveThread(thread.thread_id)
      await moderation.unpurgeThread(boardId(), thread.thread_id)
      scheduleCatalogRefetch(0)
    } finally {
      moderationBusy.value = false
    }
  }

  async function hideIdentityEntry(thread) {
    const identityHash = thread.identity_hash || thread.op_identity_hash
    if (!identityHash) return

    moderationBusy.value = true
    try {
      await moderation.hideIdentity(boardId(), identityHash)
    } finally {
      moderationBusy.value = false
    }
  }

  async function unhideIdentityEntry(thread) {
    const identityHash = thread.identity_hash || thread.op_identity_hash
    if (!identityHash) return

    moderationBusy.value = true
    try {
      await moderation.unhideIdentity(boardId(), identityHash)
    } finally {
      moderationBusy.value = false
    }
  }

  async function banIdentityEntry(thread) {
    const identityHash = thread.identity_hash || thread.op_identity_hash
    if (!identityHash) return

    moderationBusy.value = true
    try {
      const result = await moderation.banIdentity(boardId(), identityHash)
      if (result?.purged_post_ids) {
        for (const postId of result.purged_post_ids) {
          evictAndRemoveThread(postId)
        }
      }
    } finally {
      moderationBusy.value = false
    }
  }

  return {
    moderationBusy,
    threadReason,
    threadHiddenReason,
    isThreadAwaitingNetwork,
    restoreThread,
    hideThreadEntry,
    pinThreadEntry,
    unpinThreadEntry,
    purgeThreadEntry,
    unpurgeThreadEntry,
    hideIdentityEntry,
    unhideIdentityEntry,
    banIdentityEntry,
  }
}
