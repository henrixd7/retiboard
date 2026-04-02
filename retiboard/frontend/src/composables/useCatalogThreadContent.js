import { onUnmounted, ref, watch } from 'vue'
import { useAttachmentLoader } from './useAttachmentLoader.js'
import {
  summarizeAttachmentBundle,
  validateAttachmentBundle,
} from '../utils/attachmentSummary.js'
import { isLocalBannedFilePlaceholder } from '../utils/attachmentModeration.js'
import { describeAttachment } from '../utils/attachments.js'

const OP_TEXT_RETRY_DELAY_MS = 2500
const MAX_OP_TEXT_RETRY_ATTEMPTS = 6

/**
 * Owns catalog OP text/attachment state and keeps visible thread cards warm.
 */
export function useCatalogThreadContent(options) {
  const {
    boardId,
    visibleThreads,
    getLiveThread,
    settings,
    cacheStore,
    attachmentTracker,
    getText,
    getAttachments,
    fetchPayloadProgress,
    pauseAttachmentFetch,
    resumeAttachmentFetch,
    cancelAttachmentFetch,
  } = options

  const opText = ref({})
  const opAttachments = ref({})
  const opAttachmentSummaries = ref({})
  const opAttachmentIssues = ref({})
  const opTextLoadInFlight = new Set()
  const opTextRetryTimers = new Map()
  const opTextRetryAttempts = new Map()

  const {
    attachmentLoading,
    attachmentProgress,
    attachmentControllers,
    hasActiveAttachmentProgress,
    isPausedAttachmentProgress,
    isRetryableAttachmentProgress,
    maybeLoadAttachments,
    doLoadAttachments,
    pauseAttachments,
    retryAttachments,
    cancelAttachments,
    clearAttachmentState,
    abortAll,
  } = useAttachmentLoader({
    boardId,
    attachmentTracker,
    getAttachments,
    fetchPayloadProgress,
    pauseAttachmentFetch,
    resumeAttachmentFetch,
    cancelAttachmentFetch,
    getItemKey: (thread) => thread?.thread_id,
    getAttachmentHash: (thread) => thread?.op_attachment_content_hash,
    getExpectedSize: (thread) => thread?.op_attachment_payload_size || 0,
    shouldAutoRender: (size) => settings.shouldAutoRender(size),
    isLoaded: (thread) => {
      return opAttachments.value[thread?.thread_id] !== undefined
        || Boolean(opAttachmentIssues.value[thread?.thread_id])
    },
    onLoaded: (thread, result) => {
      const attachments = Array.isArray(result?.attachments) ? result.attachments : null
      if (result?.error && (!attachments || attachments.length === 0)) {
        delete opAttachmentIssues.value[thread.thread_id]
        delete opAttachments.value[thread.thread_id]
        delete opAttachmentSummaries.value[thread.thread_id]
        return false
      }

      const summary = summarizeAttachmentBundle({
        attachments,
        declaredCount: thread?.op_attachment_count || 0,
        totalSize: thread?.op_attachment_payload_size || 0,
      })
      const validation = validateAttachmentBundle({
        attachments,
        declaredCount: thread?.op_attachment_count || 0,
      })

      delete opAttachmentIssues.value[thread.thread_id]
      delete opAttachments.value[thread.thread_id]
      opAttachmentSummaries.value[thread.thread_id] = summary

      if (!validation.valid) {
        opAttachmentIssues.value[thread.thread_id] = {
          ...validation,
          summary,
        }
        return true
      }

      opAttachments.value[thread.thread_id] = attachments
      return true
    },
  })

  function clearOpTextRetry(threadId) {
    const timer = opTextRetryTimers.get(threadId)
    if (timer) clearTimeout(timer)
    opTextRetryTimers.delete(threadId)
    opTextRetryAttempts.delete(threadId)
  }

  function scheduleOpTextRetry(thread) {
    const threadId = thread?.thread_id
    if (!threadId || opTextRetryTimers.has(threadId)) return

    const attempt = (opTextRetryAttempts.get(threadId) || 0) + 1
    if (attempt > MAX_OP_TEXT_RETRY_ATTEMPTS) {
      opText.value[threadId] = '[payload unavailable]'
      clearOpTextRetry(threadId)
      return
    }

    opTextRetryAttempts.set(threadId, attempt)
    const timer = window.setTimeout(() => {
      opTextRetryTimers.delete(threadId)
      const liveThread = getLiveThread(threadId)
      if (!liveThread) return
      void loadOpText(liveThread, { force: true })
    }, OP_TEXT_RETRY_DELAY_MS)
    opTextRetryTimers.set(threadId, timer)
  }

  async function loadOpText(thread, options = {}) {
    const threadId = thread?.thread_id
    if (!threadId) return
    if (opTextLoadInFlight.has(threadId)) return
    if (!options.force && opText.value[threadId] !== undefined) return

    opTextLoadInFlight.add(threadId)
    try {
      const result = await getText(
        boardId(),
        thread.op_content_hash,
        thread.op_attachment_content_hash,
      )
      if (result?.retryable && typeof result?.text !== 'string') {
        delete opText.value[threadId]
        scheduleOpTextRetry(thread)
        return
      }

      clearOpTextRetry(threadId)
      opText.value[threadId] = result?.text || '[payload unavailable]'
    } finally {
      opTextLoadInFlight.delete(threadId)
    }
  }

  function ensureThreadContent(thread) {
    void loadOpText(thread)
    void maybeLoadAttachments(thread)
  }

  function getThreadAttachmentSummary(thread) {
    return opAttachmentSummaries.value[thread.thread_id] || summarizeAttachmentBundle({
      declaredCount: thread?.op_attachment_count || 0,
      totalSize: thread?.op_attachment_payload_size || 0,
    })
  }

  function getThreadAttachmentIssue(thread) {
    return opAttachmentIssues.value[thread.thread_id] || null
  }

  function getThreadRenderableAttachments(thread) {
    const attachments = opAttachments.value[thread.thread_id] || []
    return attachments.filter((attachment) => !isLocalBannedFilePlaceholder(attachment))
  }

  function getThreadVisibleAttachment(thread) {
    const renderableAttachments = getThreadRenderableAttachments(thread)
    if (!renderableAttachments.length) return null

    return renderableAttachments.find((attachment) => {
      return describeAttachment(attachment?.mime_type).isPreviewable
    }) || renderableAttachments[0]
  }

  function getThreadVisibleAttachmentInfo(thread) {
    const attachment = getThreadVisibleAttachment(thread)
    return attachment ? describeAttachment(attachment?.mime_type) : null
  }

  function shouldShowThreadProgress(thread) {
    const threadId = thread.thread_id
    if (opAttachments.value[threadId] || opAttachmentIssues.value[threadId]) return false

    const progress = attachmentProgress.value[threadId]
    return Boolean(attachmentLoading.value[threadId]) || hasActiveAttachmentProgress(progress)
  }

  function shouldShowThreadPaused(thread) {
    const threadId = thread.thread_id
    if (opAttachments.value[threadId] || opAttachmentIssues.value[threadId]) return false
    return isPausedAttachmentProgress(attachmentProgress.value[threadId])
  }

  function shouldShowThreadRetry(thread) {
    const threadId = thread.thread_id
    if (opAttachments.value[threadId] || opAttachmentIssues.value[threadId]) return false
    return isRetryableAttachmentProgress(attachmentProgress.value[threadId])
  }

  function shouldShowThreadLoadButton(thread) {
    const threadId = thread.thread_id
    const attachmentHash = thread.op_attachment_content_hash
    if (!attachmentHash || opAttachments.value[threadId] || opAttachmentIssues.value[threadId]) {
      return false
    }
    if (attachmentLoading.value[threadId]) return false
    if (attachmentTracker.isDownloaded(attachmentHash)) return false

    const progress = attachmentProgress.value[threadId]
    return !hasActiveAttachmentProgress(progress)
      && !isPausedAttachmentProgress(progress)
      && !isRetryableAttachmentProgress(progress)
  }

  function getThreadContentState(thread) {
    const summary = getThreadAttachmentSummary(thread)
    const issue = getThreadAttachmentIssue(thread)
    const attachments = opAttachments.value[thread.thread_id] || []
    const visibleAttachment = getThreadVisibleAttachment(thread)
    const visibleAttachmentInfo = visibleAttachment
      ? describeAttachment(visibleAttachment?.mime_type)
      : null

    return {
      text: opText.value[thread.thread_id] || '',
      attachments,
      summary,
      issue,
      visibleAttachment,
      visibleAttachmentInfo,
      progress: attachmentProgress.value[thread.thread_id] || null,
      badgeLabel: summary.badgeLabel || 'FILE',
      hasOnlyBannedLocalFiles: attachments.length > 0 && !visibleAttachment,
      showProgress: shouldShowThreadProgress(thread),
      showPaused: shouldShowThreadPaused(thread),
      showRetry: shouldShowThreadRetry(thread),
      showLoadButton: shouldShowThreadLoadButton(thread),
    }
  }

  function evictCatalogThreadState(thread) {
    const threadId = typeof thread === 'string' ? thread : thread?.thread_id
    if (!threadId) return

    if (thread && typeof thread === 'object') {
      clearAttachmentState(thread)
      if (thread.op_attachment_content_hash) {
        attachmentTracker.forgetDownloaded(thread.op_attachment_content_hash)
        cacheStore.remove(`attachments:${thread.op_attachment_content_hash}`)
      }
      if (thread.op_content_hash) {
        cacheStore.remove(`text:${thread.op_content_hash}`)
      }
    }

    delete opAttachments.value[threadId]
    delete opAttachmentSummaries.value[threadId]
    delete opAttachmentIssues.value[threadId]
    delete opText.value[threadId]
    clearOpTextRetry(threadId)
  }

  async function pauseThreadAttachments(thread) {
    const attachmentHash = thread?.op_attachment_content_hash || ''
    if (!attachmentHash) return

    await pauseAttachmentFetch(boardId(), attachmentHash)
    attachmentControllers.value[thread.thread_id]?.abort?.()
    await pauseAttachments(thread, { skipRemote: true })
  }

  async function resumeThreadAttachments(thread) {
    const attachmentHash = thread?.op_attachment_content_hash || ''
    if (!attachmentHash) return

    await resumeAttachmentFetch(boardId(), attachmentHash)
    await doLoadAttachments(thread, true)
  }

  watch(
    visibleThreads,
    (threads) => {
      for (const thread of threads) {
        if (thread?._isStub) continue
        ensureThreadContent(thread)
      }
    },
    { immediate: true },
  )

  onUnmounted(() => {
    abortAll()
    for (const timer of opTextRetryTimers.values()) {
      clearTimeout(timer)
    }
    opTextRetryTimers.clear()
    opTextRetryAttempts.clear()
  })

  return {
    loadOpText,
    ensureThreadContent,
    getThreadContentState,
    evictCatalogThreadState,
    doLoadAttachments,
    retryAttachments,
    cancelAttachments,
    pauseThreadAttachments,
    resumeThreadAttachments,
  }
}
