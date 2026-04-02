import { computed, onUnmounted, ref } from 'vue'
import { apiJson } from '../utils/api.js'

function getThreadModerationTarget(thread) {
  return {
    thread_id: thread.thread_id,
    identity_hash: thread?.identity_hash || thread?.op_identity_hash || '',
  }
}

/**
 * Owns catalog thread fetching, status polling, and the merged visible-thread
 * list derived from live API data plus local purge stubs.
 */
export function useCatalogThreads(options) {
  const {
    boardId,
    currentBucket,
    settings,
    moderation,
  } = options

  const threads = ref([])
  const loading = ref(true)
  const status = ref(null)
  let statusPollTimer = null

  const currentBoardStats = computed(() => {
    return status.value?.board_stats?.find((board) => board.board_id === boardId()) || null
  })

  const visibleThreads = computed(() => {
    const liveThreads = threads.value.filter((thread) => !moderation.isThreadPurged(thread.thread_id))
    const liveIds = new Set(liveThreads.map((thread) => thread.thread_id))
    const stubThreads = [...moderation.purgedThreadStubs.values()]
      .filter((stub) => stub?.thread_id && !liveIds.has(stub.thread_id))
      .map((stub) => ({ ...stub, _isStub: true }))
    const mergedThreads = [...liveThreads, ...stubThreads]
    const targetBucket = currentBucket.value === 'hidden' ? 'hidden' : 'main'
    const threadsById = new Map()

    for (const thread of mergedThreads) {
      if (moderation.getThreadBucket(getThreadModerationTarget(thread)) !== targetBucket) continue

      const existingThread = threadsById.get(thread.thread_id)
      if (!existingThread || thread._isStub) {
        threadsById.set(thread.thread_id, thread)
      }
    }

    const filteredThreads = [...threadsById.values()]
    filteredThreads.sort((left, right) => {
      const leftPinned = settings.isThreadPinned(boardId(), left.thread_id)
      const rightPinned = settings.isThreadPinned(boardId(), right.thread_id)
      if (leftPinned !== rightPinned) return leftPinned ? -1 : 1
      return (right.thread_last_activity || 0) - (left.thread_last_activity || 0)
    })

    return filteredThreads
  })

  const availableComposerPosts = computed(() => {
    return visibleThreads.value.map((thread) => ({
      post_id: thread.op_post_id || thread.thread_id,
      public_key: thread.public_key || '',
    }))
  })

  function getLiveThread(threadId) {
    return threads.value.find((thread) => thread.thread_id === threadId) || null
  }

  function removeThread(threadId) {
    threads.value = threads.value.filter((thread) => thread.thread_id !== threadId)
  }

  async function fetchStatus() {
    try {
      status.value = await apiJson('/api/status', { cache: 'no-store' })
    } catch {}
  }

  async function fetchCatalog(silent = false) {
    if (!silent) loading.value = true

    try {
      const fetchedThreads = await apiJson(`/api/boards/${boardId()}/posts?limit=50`)

      threads.value = fetchedThreads

      const fetchedIds = new Set(fetchedThreads.map((thread) => thread.thread_id))
      for (const [threadId] of moderation.purgedThreadStubs) {
        if (fetchedIds.has(threadId)) moderation.dropPurgedThreadStub(threadId)
      }
    } catch (error) {
      console.error('Catalog fetch failed:', error)
    } finally {
      loading.value = false
    }
  }

  async function startStatusPolling() {
    await fetchStatus()
    if (statusPollTimer) clearInterval(statusPollTimer)
    statusPollTimer = window.setInterval(fetchStatus, 10_000)
  }

  function stopStatusPolling() {
    if (!statusPollTimer) return
    clearInterval(statusPollTimer)
    statusPollTimer = null
  }

  onUnmounted(() => {
    stopStatusPolling()
  })

  return {
    threads,
    loading,
    status,
    currentBoardStats,
    visibleThreads,
    availableComposerPosts,
    getLiveThread,
    removeThread,
    fetchStatus,
    fetchCatalog,
    startStatusPolling,
    stopStatusPolling,
  }
}
