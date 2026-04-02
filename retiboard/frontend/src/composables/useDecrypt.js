/**
 * Composable for split-blob payload decryption.
 *
 * getText()  — always fetches + decrypts the text blob (small)
 * getAttachments() — fetches + decrypts the attachment blob on demand (potentially large)
 *
 * Both use the LRU cache. Text and attachment payloads are cached independently
 * by their content_hash / attachment_content_hash.
 */
import { onUnmounted } from 'vue'
import { useBoardStore } from '../stores/boardStore.js'
import { useCacheStore } from '../stores/cacheStore.js'
import { useModerationStore } from '../stores/moderationStore.js'
import { useSettingsStore } from '../stores/settingsStore.js'
import { decryptTextPayload, decryptAttachmentPayloadLowMem, decryptPayload, verifyContentHash, fetchAndVerify } from '../crypto/decrypt.js'
import { sanitizeAttachmentListForLocalBans } from '../utils/attachmentModeration.js'
import { apiJsonResponse, apiOk } from '../utils/api.js'

export function useDecrypt() {
  const boardStore = useBoardStore()
  const cacheStore = useCacheStore()
  const moderationStore = useModerationStore()
  const settingsStore = useSettingsStore()
  const activePollers = new Set()
  const PROGRESS_404_STOP_THRESHOLD = 5

  function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms))
  }

  function buildLocalProgress(boardId, contentHash, state, lastError = '') {
    return {
      board_id: boardId,
      blob_hash: contentHash,
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
      updated_at: Math.floor(Date.now() / 1000),
    }
  }

  async function fetchPayloadProgressDetail(boardId, contentHash) {
    const { response, data } = await apiJsonResponse(
      `/api/boards/${boardId}/payloads/${contentHash}/progress`,
      {
        cache: 'no-store',
        throwOnError: false,
        timeoutMs: 4_000,
      },
    )
    if (!response?.ok) {
      return { ok: false, status: response?.status || 0, data: null }
    }
    return { ok: true, status: response.status, data }
  }

  async function fetchPayloadProgress(boardId, contentHash) {
    const detail = await fetchPayloadProgressDetail(boardId, contentHash)
    if (!detail.ok) return null
    return detail.data
  }

  function isTerminalProgress(progress) {
    const state = String(progress?.state || '')
    return progress?.complete || ['failed', 'cancelled', 'complete', 'stopped'].includes(state)
  }

  function stoppedProgress(boardId, contentHash) {
    return buildLocalProgress(
      boardId,
      contentHash,
      'stopped',
      'No active fetch session',
    )
  }

  function startingProgress(boardId, contentHash) {
    return buildLocalProgress(boardId, contentHash, 'starting', '')
  }

  function failedProgress(boardId, contentHash, error) {
    return buildLocalProgress(boardId, contentHash, 'failed', error)
  }

  function completeProgress(boardId, contentHash) {
    return {
      ...buildLocalProgress(boardId, contentHash, 'complete', ''),
      percent_complete: 100,
      complete: true,
    }
  }

  async function pollPayloadProgress(boardId, contentHash, onProgress, intervalMs = 700) {
    let stopped = false
    let consecutiveNotFound = 0
    // Increased threshold to account for slow path resolution on RNS.
    const MAX_404 = 15 

    const loop = (async () => {
      while (!stopped) {
        try {
          const detail = await fetchPayloadProgressDetail(boardId, contentHash)
          if (detail.ok && detail.data) {
            consecutiveNotFound = 0
            onProgress?.(detail.data)
            if (isTerminalProgress(detail.data)) break
          } else if (detail.status === 404) {
            consecutiveNotFound += 1
            
            // v3.6.3: Only stop if we've hit a high threshold. 
            // Small payloads use the legacy path which has NO /progress endpoint,
            // so we must be patient while the main /payloads/{hash} request is pending.
            if (consecutiveNotFound >= MAX_404) {
              // We don't mark as STOPPED here anymore, we just stop polling noise.
              // The primary fetch promise in decryptAndCachePayload will handle the final result.
              break
            }
          }
        } catch {
          // Ignore transient errors.
        }

        if (!stopped) {
          await sleep(intervalMs)
        }
      }
    })()

    const poller = {
      stop: async () => {
        stopped = true
        try {
          await loop
        } catch {
          // Ignore polling loop shutdown errors.
        }
      },
    }
    activePollers.add(poller)
    return poller
  }

  function getManualAttachmentLimitBytes() {
    const configured = Number(settingsStore.maxManualLoadBytes ?? 0)
    return configured > 0 ? configured : 0
  }

  async function stopPoller(poller) {
    if (!poller) return
    activePollers.delete(poller)
    await poller.stop?.()
  }

  onUnmounted(() => {
    for (const poller of Array.from(activePollers)) {
      poller.stop?.().catch?.(() => {})
      activePollers.delete(poller)
    }
  })

  function sanitizeAttachmentResult(result) {
    if (!result?.attachments) return result
    const sanitized = sanitizeAttachmentListForLocalBans(
      result.attachments,
      (fileHash) => moderationStore.isFileHashBanned(fileHash),
    )
    if (!sanitized.changed) return result
    return {
      ...result,
      attachments: sanitized.attachments,
      contains_banned_local_files: sanitized.hasBannedFiles,
    }
  }

  async function getText(boardId, contentHash, attachmentContentHash = '') {
    const cacheKey = 'text:' + contentHash
    const cached = cacheStore.get(cacheKey)
    if (cached) {
      const sanitizedCached = sanitizeAttachmentResult(cached)
      if (sanitizedCached !== cached) {
        cacheStore.set(cacheKey, sanitizedCached)
      }
      return sanitizedCached
    }

    try {
      const token = sessionStorage.getItem('retiboard_token')
      const headers = new Headers()
      if (token) {
        headers.set('X-RetiBoard-Token', token)
      }

      const res = await fetch(`/api/boards/${boardId}/payloads/${contentHash}`, {
        headers,
        cache: 'no-store'
      })
      if (!res.ok) {
        return {
          text: null,
          error: 'Payload unavailable',
          status: res.status,
          retryable: [404, 408, 425, 429, 500, 502, 503, 504].includes(res.status),
        }
      }

      const blob = new Uint8Array(await res.arrayBuffer())
      if (!(await verifyContentHash(blob, contentHash))) {
        return { text: null, error: 'Hash mismatch', retryable: false }
      }

      const key = await boardStore.getBoardKey(boardId)
      if (attachmentContentHash) {
        const result = await decryptTextPayload(key, blob)
        if (!result.error) {
          cacheStore.set(cacheKey, result)
        }
        return result
      }

      const result = sanitizeAttachmentResult(await decryptPayload(key, blob))
      if (!result.error) {
        cacheStore.set(cacheKey, result)
      }
      return result
    } catch (e) {
      return { text: null, error: e.message, retryable: true }
    }
  }

  async function getAttachments(boardId, attachmentContentHash, options = {}) {
    if (!attachmentContentHash) return { attachments: null, error: 'No attachments' }

    const cacheKey = 'attachments:' + attachmentContentHash
    const cached = cacheStore.get(cacheKey)
    if (cached) {
      const sanitizedCached = sanitizeAttachmentResult(cached)
      if (sanitizedCached !== cached) {
        cacheStore.set(cacheKey, sanitizedCached)
      }
      return sanitizedCached
    }

    let progressPoller = null

    try {
      const existingProgress = await fetchPayloadProgress(boardId, attachmentContentHash)
      options.onProgress?.(existingProgress || {
        ...startingProgress(boardId, attachmentContentHash),
      })

      progressPoller = await pollPayloadProgress(boardId, attachmentContentHash, options.onProgress)

      const manualLimit = getManualAttachmentLimitBytes()
      const expectedSize = Number(options.expectedSize || 0)
      if (manualLimit > 0 && expectedSize > 0 && expectedSize > manualLimit) {
        return {
          attachments: null,
          error: `Attachments exceed manual load limit (${settingsStore.prettySize(manualLimit)})`,
        }
      }

      const query = options.manual ? '?manual=1' : ''
      const { data: blob, error: fetchError, contentLength } = await fetchAndVerify(
        `/api/boards/${boardId}/payloads/${attachmentContentHash}${query}`,
        attachmentContentHash,
        {
          signal: options.signal,
          maxBytes: manualLimit > 0 ? manualLimit : undefined,
        },
      )
      if (fetchError || !blob) {
        return {
          attachments: null,
          error: fetchError === 'Hash mismatch'
            ? 'Attachment hash mismatch'
            : (fetchError || 'Attachments unavailable'),
        }
      }

      if (manualLimit > 0 && contentLength > 0 && contentLength > manualLimit) {
        return {
          attachments: null,
          error: `Attachments exceed manual load limit (${settingsStore.prettySize(manualLimit)})`,
        }
      }

      const key = await boardStore.getBoardKey(boardId)
      const result = await decryptAttachmentPayloadLowMem(key, blob)
      const sanitizedResult = sanitizeAttachmentResult(result)

      if (!sanitizedResult.error) {
        cacheStore.set(cacheKey, sanitizedResult)
        options.onProgress?.(completeProgress(boardId, attachmentContentHash))
      }
      return sanitizedResult
    } catch (e) {
      if (e?.name === 'AbortError') {
        const existingProgress = await fetchPayloadProgress(boardId, attachmentContentHash)
        if (existingProgress) {
          options.onProgress?.(existingProgress)
        }
        return { attachments: null, error: 'aborted', aborted: true }
      }
      options.onProgress?.(failedProgress(boardId, attachmentContentHash, e.message))
      return { attachments: null, error: e.message }
    } finally {
      await stopPoller(progressPoller)
    }
  }

  async function getPayload(boardId, contentHash) {
    return getText(boardId, contentHash, '')
  }

  async function pauseAttachmentFetch(boardId, attachmentContentHash) {
    return apiOk(`/api/boards/${boardId}/payloads/${attachmentContentHash}/pause`, {
      method: 'POST',
    })
  }

  async function resumeAttachmentFetch(boardId, attachmentContentHash) {
    return apiOk(`/api/boards/${boardId}/payloads/${attachmentContentHash}/resume`, {
      method: 'POST',
    })
  }

  async function cancelAttachmentFetch(boardId, attachmentContentHash) {
    return apiOk(`/api/boards/${boardId}/payloads/${attachmentContentHash}/fetch`, {
      method: 'DELETE',
    })
  }

  return {
    getText,
    getAttachments,
    getPayload,
    pauseAttachmentFetch,
    resumeAttachmentFetch,
    cancelAttachmentFetch,
    fetchPayloadProgress,
  }
}
