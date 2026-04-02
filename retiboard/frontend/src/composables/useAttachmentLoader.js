import { onUnmounted, ref } from 'vue'

const AUTO_FETCH_LIMIT = 10
const BASE_FAILURE_COOLDOWN_MS = 30_000
const MAX_FAILURE_COOLDOWN_MS = 5 * 60_000

let activeAutoFetchSlots = 0
const autoFetchWaiters = []
const blobFailureCooldowns = new Map()

function createAbortError() {
  const error = new Error('Aborted')
  error.name = 'AbortError'
  return error
}

function pumpAutoFetchWaiters() {
  while (activeAutoFetchSlots < AUTO_FETCH_LIMIT && autoFetchWaiters.length) {
    const waiter = autoFetchWaiters.shift()
    if (!waiter || waiter.signal?.aborted) {
      waiter?.cleanup?.()
      continue
    }
    waiter.grant()
  }
}

function acquireAutoFetchSlot(signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(createAbortError())
      return
    }

    let settled = false
    let waiter = null

    const cleanup = () => {
      if (!waiter) return
      const idx = autoFetchWaiters.indexOf(waiter)
      if (idx >= 0) autoFetchWaiters.splice(idx, 1)
      if (waiter.signal && waiter.onAbort) {
        waiter.signal.removeEventListener('abort', waiter.onAbort)
      }
      waiter = null
    }

    const grant = () => {
      if (settled) return
      settled = true
      cleanup()
      activeAutoFetchSlots += 1
      let released = false
      resolve(() => {
        if (released) return
        released = true
        activeAutoFetchSlots = Math.max(0, activeAutoFetchSlots - 1)
        pumpAutoFetchWaiters()
      })
    }

    if (activeAutoFetchSlots < AUTO_FETCH_LIMIT) {
      grant()
      return
    }

    waiter = {
      signal,
      grant,
      cleanup,
      onAbort: () => {
        if (settled) return
        settled = true
        cleanup()
        reject(createAbortError())
      },
    }
    if (signal) {
      signal.addEventListener('abort', waiter.onAbort, { once: true })
    }
    autoFetchWaiters.push(waiter)
  })
}

function attachmentBlobKey(boardId, hash) {
  return `${boardId}:${hash}`
}

function isRetryableUnavailableError(error) {
  const normalized = String(error || '').trim().toLowerCase()
  return (
    normalized.includes('404')
    || normalized.includes('not found')
    || normalized.includes('unavailable')
    || normalized.includes('no active fetch session')
  )
}

function clearBlobFailureCooldown(blobKey) {
  blobFailureCooldowns.delete(blobKey)
}

function getBlobFailureCooldown(blobKey) {
  const entry = blobFailureCooldowns.get(blobKey) || null
  if (!entry) return null
  if (entry.until <= Date.now()) {
    blobFailureCooldowns.delete(blobKey)
    return null
  }
  return entry
}

function noteBlobFailureCooldown(blobKey, error) {
  const previous = getBlobFailureCooldown(blobKey)
  const attempt = Math.min((previous?.attempt || 0) + 1, 5)
  const cooldownMs = Math.min(
    BASE_FAILURE_COOLDOWN_MS * (2 ** Math.max(0, attempt - 1)),
    MAX_FAILURE_COOLDOWN_MS,
  )
  const entry = {
    attempt,
    until: Date.now() + cooldownMs,
    last_error: String(error || '').trim(),
  }
  blobFailureCooldowns.set(blobKey, entry)
  return entry
}

function buildAttachmentProgress({
  boardId,
  blobHash,
  state,
  lastError = '',
  retryAt = 0,
  queuePosition = 0,
}) {
  return {
    board_id: boardId,
    blob_hash: blobHash,
    state,
    percent_complete: 0,
    stored_chunks: 0,
    chunk_count: 0,
    active_requests: 0,
    available_peers: 0,
    cooled_down_peers: 0,
    resumed_from_persisted: false,
    complete: false,
    last_error: lastError,
    retry_at: retryAt,
    queue_position: queuePosition,
    updated_at: Math.floor(Date.now() / 1000),
  }
}

export const __attachmentLoaderInternals = {
  AUTO_FETCH_LIMIT,
  acquireAutoFetchSlot,
  buildAttachmentProgress,
  clearBlobFailureCooldown,
  getBlobFailureCooldown,
  isRetryableUnavailableError,
  noteBlobFailureCooldown,
  resetForTests() {
    activeAutoFetchSlots = 0
    autoFetchWaiters.length = 0
    blobFailureCooldowns.clear()
  },
}

/**
 * Shared attachment-fetch lifecycle for views that restore persisted progress
 * and attach view-specific rendering when decrypted attachments arrive.
 *
 * Unmount only detaches the current view request. It does not cancel the
 * underlying fetch session, so long-running downloads can be resumed later.
 */
export function useAttachmentLoader(options) {
  const {
    boardId,
    attachmentTracker,
    getAttachments,
    fetchPayloadProgress,
    pauseAttachmentFetch,
    resumeAttachmentFetch,
    cancelAttachmentFetch,
    getItemKey,
    getAttachmentHash,
    getExpectedSize,
    shouldAutoRender,
    isLoaded,
    onLoaded,
  } = options

  const attachmentLoading = ref({})
  const attachmentProgress = ref({})
  const attachmentControllers = ref({})
  const requestTokens = ref({})

  function resolveBoardId() {
    return typeof boardId === 'function' ? boardId() : boardId
  }

  function itemKey(item) {
    return getItemKey(item)
  }

  function attachmentHash(item) {
    return getAttachmentHash(item)
  }

  function expectedSize(item) {
    return Number(getExpectedSize?.(item) || 0)
  }

  function hasActiveAttachmentProgress(progress) {
    if (!progress) return false
    return !['paused', 'failed', 'complete', 'cancelled', 'stopped', 'cooldown'].includes(progress.state || '')
  }

  function isPausedAttachmentProgress(progress) {
    return progress?.state === 'paused'
  }

  function isRetryableAttachmentProgress(progress) {
    return ['failed', 'cancelled', 'stopped', 'cooldown'].includes(progress?.state || '')
  }

  function currentBoardId() {
    return String(resolveBoardId() || '')
  }

  function currentBlobKey(hash) {
    return attachmentBlobKey(currentBoardId(), hash)
  }

  function buildLocalProgress(hash, state, lastError = '', retryAt = 0, queuePosition = 0) {
    return buildAttachmentProgress({
      boardId: currentBoardId(),
      blobHash: hash,
      state,
      lastError,
      retryAt,
      queuePosition,
    })
  }

  function applyCooldownProgress(key, hash) {
    const cooldown = getBlobFailureCooldown(currentBlobKey(hash))
    if (!cooldown) return false
    attachmentProgress.value[key] = buildLocalProgress(
      hash,
      'cooldown',
      cooldown.last_error || 'Attachment unavailable',
      cooldown.until,
    )
    return true
  }

  function clearAttachmentState(item, options = {}) {
    const key = itemKey(item)
    if (!key) return

    if (options.abort !== false) {
      attachmentControllers.value[key]?.abort?.()
    }

    delete attachmentControllers.value[key]
    delete attachmentLoading.value[key]
    delete attachmentProgress.value[key]
    delete requestTokens.value[key]
  }

  function abortAll() {
    for (const controller of Object.values(attachmentControllers.value)) {
      controller?.abort?.()
    }

    attachmentControllers.value = {}
    attachmentLoading.value = {}
    attachmentProgress.value = {}
    requestTokens.value = {}
  }

  async function hydrateAttachmentState(item) {
    const key = itemKey(item)
    const hash = attachmentHash(item)
    if (!key || !hash || isLoaded(item) || attachmentLoading.value[key]) return false
    if (applyCooldownProgress(key, hash)) return true

    const progress = await fetchPayloadProgress(resolveBoardId(), hash)
    if (!progress) return false

    if (progress.complete || progress.state === 'complete') {
      await doLoadAttachments(item, false)
      return true
    }

    if (hasActiveAttachmentProgress(progress) || isPausedAttachmentProgress(progress)) {
      attachmentProgress.value[key] = progress
      return true
    }

    delete attachmentProgress.value[key]
    return false
  }

  async function maybeLoadAttachments(item) {
    const key = itemKey(item)
    const hash = attachmentHash(item)
    if (!key || !hash || isLoaded(item)) return

    const existingProgress = attachmentProgress.value[key]
    if (hasActiveAttachmentProgress(existingProgress) || isPausedAttachmentProgress(existingProgress) || isRetryableAttachmentProgress(existingProgress)) {
      return
    }
    if (applyCooldownProgress(key, hash)) return

    if (attachmentTracker.isDownloaded(hash)) {
      await doLoadAttachments(item, false)
      return
    }

    const restored = await hydrateAttachmentState(item)
    if (restored) return
    if (!shouldAutoRender(expectedSize(item))) return
    await doLoadAttachments(item, false)
  }

  async function doLoadAttachments(item, manual = false, options = {}) {
    const key = itemKey(item)
    const hash = attachmentHash(item)
    if (!key || !hash || isLoaded(item)) return
    if (attachmentControllers.value[key]) return

    const bypassCooldown = Boolean(options.bypassCooldown)
      || (manual && attachmentProgress.value[key]?.state === 'paused')
    const blobKey = currentBlobKey(hash)
    if (!bypassCooldown && applyCooldownProgress(key, hash)) return

    const controller = new AbortController()
    const token = Symbol(String(key))
    attachmentControllers.value[key] = controller
    attachmentLoading.value[key] = false
    requestTokens.value[key] = token

    let result = null
    let releaseAutoFetchSlot = null
    try {
      if (!manual) {
        const queuePosition = activeAutoFetchSlots < AUTO_FETCH_LIMIT ? 0 : (autoFetchWaiters.length + 1)
        if (queuePosition > 0) {
          attachmentProgress.value[key] = buildLocalProgress(hash, 'queued', '', 0, queuePosition)
        }
        releaseAutoFetchSlot = await acquireAutoFetchSlot(controller.signal)
      }

      if (requestTokens.value[key] !== token) return

      attachmentLoading.value[key] = true
      if (!manual && attachmentProgress.value[key]?.state === 'queued') {
        attachmentProgress.value[key] = buildLocalProgress(hash, 'starting')
      }

      result = await getAttachments(resolveBoardId(), hash, {
        signal: controller.signal,
        expectedSize: expectedSize(item),
        manual,
        onProgress: (progress) => {
          if (requestTokens.value[key] !== token) return
          attachmentProgress.value[key] = progress
        },
      })
    } catch (error) {
      if (error?.name === 'AbortError') {
        result = { attachments: null, error: 'aborted', aborted: true }
      } else {
        throw error
      }
    } finally {
      releaseAutoFetchSlot?.()
      if (requestTokens.value[key] === token) {
        delete attachmentControllers.value[key]
        attachmentLoading.value[key] = false
      }
    }

    if (requestTokens.value[key] !== token) return

    const loaded = await onLoaded(item, result)
    if (loaded) {
      clearBlobFailureCooldown(blobKey)
      attachmentTracker.markDownloaded(hash)
      delete attachmentProgress.value[key]
      delete requestTokens.value[key]
      return
    }

    if (result?.aborted && !['paused', 'cancelled'].includes(attachmentProgress.value[key]?.state || '')) {
      attachmentProgress.value[key] = {
        ...(attachmentProgress.value[key] || {}),
        state: 'paused',
        last_error: '',
      }
      delete requestTokens.value[key]
      return
    }

    if (!result?.aborted && attachmentProgress.value[key]?.state !== 'paused') {
      attachmentTracker.forgetDownloaded(hash)
      const error = String(result?.error || '').trim()
      if (isRetryableUnavailableError(error)) {
        const cooldown = noteBlobFailureCooldown(blobKey, error)
        attachmentProgress.value[key] = buildLocalProgress(
          hash,
          manual ? 'stopped' : 'cooldown',
          error || 'Attachment unavailable',
          cooldown.until,
        )
      } else {
        attachmentProgress.value[key] = buildLocalProgress(
          hash,
          'failed',
          error || 'Attachment fetch failed',
        )
      }
    }

    delete requestTokens.value[key]
  }

  async function pauseAttachments(item, options = {}) {
    const key = itemKey(item)
    const hash = attachmentHash(item)
    if (!key || !hash) return

    clearBlobFailureCooldown(currentBlobKey(hash))
    if (!options.skipRemote) {
      await pauseAttachmentFetch(resolveBoardId(), hash)
    }
    attachmentControllers.value[key]?.abort?.()
    attachmentLoading.value[key] = false
    attachmentProgress.value[key] = {
      ...(attachmentProgress.value[key] || {}),
      board_id: resolveBoardId(),
      blob_hash: hash,
      state: 'paused',
      last_error: '',
    }
  }

  async function resumeAttachments(item) {
    const key = itemKey(item)
    const hash = attachmentHash(item)
    if (!key || !hash) return

    clearBlobFailureCooldown(currentBlobKey(hash))
    await resumeAttachmentFetch(resolveBoardId(), hash)
    await doLoadAttachments(item, true, { bypassCooldown: true })
  }

  async function retryAttachments(item) {
    const key = itemKey(item)
    const hash = attachmentHash(item)
    if (!key || !hash) return

    clearBlobFailureCooldown(currentBlobKey(hash))
    clearAttachmentState(item)
    attachmentProgress.value[key] = buildLocalProgress(hash, 'starting')
    await doLoadAttachments(item, true, { bypassCooldown: true })
  }

  async function cancelAttachments(item) {
    const key = itemKey(item)
    const hash = attachmentHash(item)
    if (!key || !hash) return

    clearBlobFailureCooldown(currentBlobKey(hash))
    attachmentProgress.value[key] = buildLocalProgress(hash, 'cancelled')
    attachmentControllers.value[key]?.abort?.()
    await cancelAttachmentFetch(resolveBoardId(), hash)
    attachmentLoading.value[key] = false
    delete requestTokens.value[key]
  }

  onUnmounted(abortAll)

  return {
    attachmentLoading,
    attachmentProgress,
    attachmentControllers,
    hasActiveAttachmentProgress,
    isPausedAttachmentProgress,
    isRetryableAttachmentProgress,
    hydrateAttachmentState,
    maybeLoadAttachments,
    doLoadAttachments,
    pauseAttachments,
    resumeAttachments,
    retryAttachments,
    cancelAttachments,
    clearAttachmentState,
    abortAll,
  }
}
