/**
 * Attachment download tracker (Pinia) — in-memory only (§17).
 */
import { defineStore } from 'pinia'
import { reactive } from 'vue'

const MAX_TRACKED_ATTACHMENTS = 5000

export const useAttachmentTracker = defineStore('attachmentTracker', () => {
  const downloaded = reactive(new Set())
  const order = []

  function markDownloaded(attachmentContentHash) {
    if (!attachmentContentHash || downloaded.has(attachmentContentHash)) return
    downloaded.add(attachmentContentHash)
    order.push(attachmentContentHash)
    while (order.length > MAX_TRACKED_ATTACHMENTS) {
      const oldest = order.shift()
      if (oldest) downloaded.delete(oldest)
    }
  }

  function isDownloaded(attachmentContentHash) {
    return attachmentContentHash && downloaded.has(attachmentContentHash)
  }

  function forgetDownloaded(attachmentContentHash) {
    if (!attachmentContentHash || !downloaded.has(attachmentContentHash)) return
    downloaded.delete(attachmentContentHash)
    const index = order.indexOf(attachmentContentHash)
    if (index !== -1) order.splice(index, 1)
  }

  function clear() {
    downloaded.clear()
    order.splice(0, order.length)
  }

  return { markDownloaded, isDownloaded, forgetDownloaded, clear }
})
