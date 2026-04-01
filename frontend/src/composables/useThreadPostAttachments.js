import { onUnmounted, ref } from 'vue'
import { useAttachmentLoader } from './useAttachmentLoader.js'
import {
  summarizeAttachmentBundle,
  validateAttachmentBundle,
} from '../utils/attachmentSummary.js'
import {
  buildBannedFilePlaceholder,
  isLocalBannedFilePlaceholder,
} from '../utils/attachmentModeration.js'

function createAttachmentSource(attachment) {
  if (attachment?.blob) return attachment.blob
  if (attachment?.bytes) {
    return new Blob([attachment.bytes], { type: attachment.mime_type })
  }
  return null
}

function normalizeFileHash(attachment) {
  return String(attachment?.file_hash || '').trim().toLowerCase()
}

/**
 * Owns per-post attachment render state, progress predicates, and overlay
 * object URL lifecycle for the thread view.
 */
export function useThreadPostAttachments(options) {
  const {
    boardId,
    settings,
    moderation,
    cacheStore,
    attachmentTracker,
    getAttachments,
    fetchPayloadProgress,
    pauseAttachmentFetch,
    resumeAttachmentFetch,
    cancelAttachmentFetch,
  } = options

  const postAttachments = ref({})
  const postAttachmentSummaries = ref({})
  const postAttachmentIssues = ref({})
  const overlayAttachment = ref(null)
  const activeOverlayUrls = new Set()

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
  } = useAttachmentLoader({
    boardId,
    attachmentTracker,
    getAttachments,
    fetchPayloadProgress,
    pauseAttachmentFetch,
    resumeAttachmentFetch,
    cancelAttachmentFetch,
    getItemKey: (post) => post?.post_id,
    getAttachmentHash: (post) => post?.attachment_content_hash,
    getExpectedSize: (post) => post?.attachment_payload_size || 0,
    shouldAutoRender: (size) => settings.shouldAutoRender(size),
    isLoaded: (post) => {
      return postAttachments.value[post?.post_id] !== undefined
        || Boolean(postAttachmentIssues.value[post?.post_id])
    },
    onLoaded: (post, result) => {
      const attachments = Array.isArray(result?.attachments) ? result.attachments : null
      if (result?.error && (!attachments || attachments.length === 0)) {
        delete postAttachmentIssues.value[post.post_id]
        delete postAttachments.value[post.post_id]
        delete postAttachmentSummaries.value[post.post_id]
        return false
      }

      const summary = summarizeAttachmentBundle({
        attachments,
        declaredCount: post?.attachment_count || 0,
        totalSize: post?.attachment_payload_size || 0,
      })
      const validation = validateAttachmentBundle({
        attachments,
        declaredCount: post?.attachment_count || 0,
      })

      delete postAttachmentIssues.value[post.post_id]
      delete postAttachments.value[post.post_id]
      postAttachmentSummaries.value[post.post_id] = summary

      if (!validation.valid) {
        postAttachmentIssues.value[post.post_id] = {
          ...validation,
          summary,
        }
        return true
      }

      postAttachments.value[post.post_id] = attachments
      return true
    },
  })

  function revokeOverlayUrl(url) {
    if (!url) return
    try { URL.revokeObjectURL(url) } catch {}
    activeOverlayUrls.delete(url)
  }

  function closeOverlay() {
    revokeOverlayUrl(overlayAttachment.value?.src)
    overlayAttachment.value = null
  }

  function closeOverlayForPost(postId) {
    if (overlayAttachment.value?.postId !== postId) return
    closeOverlay()
  }

  function openOverlay(post, attachment) {
    const source = createAttachmentSource(attachment)
    if (!source) return

    closeOverlay()
    const src = URL.createObjectURL(source)
    activeOverlayUrls.add(src)
    overlayAttachment.value = {
      src,
      postId: post?.post_id || null,
      fileHash: normalizeFileHash(attachment),
      mimeType: attachment?.mime_type || '',
    }
  }

  function getPostAttachmentSummary(post) {
    return postAttachmentSummaries.value[post.post_id] || summarizeAttachmentBundle({
      declaredCount: post?.attachment_count || 0,
      totalSize: post?.attachment_payload_size || 0,
    })
  }

  function getPostAttachmentIssue(post) {
    return postAttachmentIssues.value[post.post_id] || null
  }

  function getRenderablePostAttachments(post) {
    const attachments = postAttachments.value[post.post_id] || []
    return attachments.filter((attachment) => {
      if (!isLocalBannedFilePlaceholder(attachment)) return true
      return !moderation.isBannedFilePlaceholderHidden(attachment.file_hash)
    })
  }

  function postHasVisibleAttachments(post) {
    return getRenderablePostAttachments(post).length > 0
  }

  function postHasOnlyHiddenBannedFiles(post) {
    const attachments = postAttachments.value[post.post_id] || []
    if (!attachments.length) return false

    return attachments.every((attachment) => {
      return isLocalBannedFilePlaceholder(attachment)
        && moderation.isBannedFilePlaceholderHidden(attachment.file_hash)
    })
  }

  function shouldShowPostAttachmentProgress(post) {
    if (postAttachments.value[post.post_id] !== undefined) return false
    if (postAttachmentIssues.value[post.post_id]) return false

    const progress = attachmentProgress.value[post.post_id]
    return Boolean(attachmentLoading.value[post.post_id]) || hasActiveAttachmentProgress(progress)
  }

  function shouldShowPostAttachmentPaused(post) {
    if (postAttachments.value[post.post_id] !== undefined) return false
    if (postAttachmentIssues.value[post.post_id]) return false
    return isPausedAttachmentProgress(attachmentProgress.value[post.post_id])
  }

  function shouldShowPostAttachmentRetry(post) {
    if (postAttachments.value[post.post_id] !== undefined) return false
    if (postAttachmentIssues.value[post.post_id]) return false
    return isRetryableAttachmentProgress(attachmentProgress.value[post.post_id])
  }

  function shouldShowPostLoadAttachments(post) {
    if (!post.attachment_content_hash) return false
    if (Array.isArray(postAttachments.value[post.post_id])) return false
    if (postAttachmentIssues.value[post.post_id]) return false
    if (attachmentLoading.value[post.post_id]) return false
    if (attachmentTracker.isDownloaded(post.attachment_content_hash)) return false

    const progress = attachmentProgress.value[post.post_id]
    return !hasActiveAttachmentProgress(progress)
      && !isPausedAttachmentProgress(progress)
      && !isRetryableAttachmentProgress(progress)
  }

  function getPostAttachmentState(post) {
    const summary = getPostAttachmentSummary(post)
    const issue = getPostAttachmentIssue(post)

    return {
      attachments: postAttachments.value[post.post_id] || [],
      renderableAttachments: getRenderablePostAttachments(post),
      summary,
      issue,
      progress: attachmentProgress.value[post.post_id] || null,
      hasVisibleAttachments: postHasVisibleAttachments(post),
      hasOnlyHiddenBannedFiles: postHasOnlyHiddenBannedFiles(post),
      showProgress: shouldShowPostAttachmentProgress(post),
      showPaused: shouldShowPostAttachmentPaused(post),
      showRetry: shouldShowPostAttachmentRetry(post),
      showLoadButton: shouldShowPostLoadAttachments(post),
    }
  }

  function setInlineAttachments(post, attachments) {
    postAttachments.value[post.post_id] = attachments
    postAttachmentSummaries.value[post.post_id] = summarizeAttachmentBundle({
      attachments,
      declaredCount: attachments.length,
      totalSize: 0,
    })
  }

  function evictThreadPostAttachments(post) {
    if (!post?.post_id) return

    const postId = post.post_id
    const attachmentHash = post.attachment_content_hash
    closeOverlayForPost(postId)
    clearAttachmentState(post)
    delete postAttachments.value[postId]
    delete postAttachmentSummaries.value[postId]
    delete postAttachmentIssues.value[postId]

    if (attachmentHash) attachmentTracker.forgetDownloaded(attachmentHash)
    if (attachmentHash) cacheStore.remove(`attachments:${attachmentHash}`)
  }

  function replaceBannedFileInPost(post, fileHash) {
    const attachments = postAttachments.value[post.post_id] || []
    postAttachments.value[post.post_id] = attachments.map((attachment) => {
      if (normalizeFileHash(attachment) !== fileHash) return attachment
      if (isLocalBannedFilePlaceholder(attachment)) return attachment
      if (
        overlayAttachment.value?.postId === post.post_id
        && overlayAttachment.value?.fileHash === fileHash
      ) {
        closeOverlay()
      }
      return buildBannedFilePlaceholder(attachment)
    })
  }

  async function banFileLocally(post, attachment) {
    const fileHash = normalizeFileHash(attachment)
    if (!fileHash) return

    moderation.banFileHash(fileHash)
    cacheStore.removeByPrefix('attachments:')
    replaceBannedFileInPost(post, fileHash)
  }

  function hideBannedFilePlaceholder(attachment) {
    const fileHash = normalizeFileHash(attachment)
    if (!fileHash) return
    moderation.hideBannedFilePlaceholder(fileHash)
  }

  async function reloadPostAttachments(post) {
    closeOverlayForPost(post.post_id)
    delete postAttachments.value[post.post_id]
    delete postAttachmentIssues.value[post.post_id]
    clearAttachmentState(post, { abort: false })
    cacheStore.removeByPrefix('attachments:')
    await doLoadAttachments(post, true)
  }

  async function unbanLocalFileFromPost(post, attachment) {
    const fileHash = normalizeFileHash(attachment)
    if (!fileHash) return

    moderation.showBannedFilePlaceholder(fileHash)
    moderation.unbanFileHash(fileHash)
    await reloadPostAttachments(post)
  }

  async function pausePostAttachments(post) {
    const attachmentHash = post?.attachment_content_hash || ''
    if (!attachmentHash) return

    await pauseAttachmentFetch(boardId(), attachmentHash)
    attachmentControllers.value[post.post_id]?.abort?.()
    await pauseAttachments(post, { skipRemote: true })
  }

  async function resumePostAttachments(post) {
    const attachmentHash = post?.attachment_content_hash || ''
    if (!attachmentHash) return

    await resumeAttachmentFetch(boardId(), attachmentHash)
    await doLoadAttachments(post, true)
  }

  function clearAllAttachmentState() {
    closeOverlay()
    postAttachments.value = {}
    postAttachmentSummaries.value = {}
    postAttachmentIssues.value = {}
  }

  onUnmounted(() => {
    closeOverlay()
    for (const url of activeOverlayUrls) {
      revokeOverlayUrl(url)
    }
  })

  return {
    overlayAttachment,
    maybeLoadAttachments,
    doLoadAttachments,
    retryAttachments,
    cancelAttachments,
    getPostAttachmentState,
    setInlineAttachments,
    evictThreadPostAttachments,
    openOverlay,
    closeOverlay,
    banFileLocally,
    hideBannedFilePlaceholder,
    unbanLocalFileFromPost,
    pausePostAttachments,
    resumePostAttachments,
    clearAllAttachmentState,
  }
}
