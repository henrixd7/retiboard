/**
 * Notification store (Pinia) — in-memory only (§17).
 *
 * Tracks posts created by this client session and generates
 * notifications when encrypted private pings reference them.
 *
 * All state is ephemeral — lost on tab close. No persistent storage.
 */
import { defineStore } from 'pinia'
import { ref, computed, markRaw } from 'vue'
import { decryptPing } from '../crypto/pings.js'

export const useNotificationStore = defineStore('notifications', () => {
  // Set of post_id hashes we created this session.
  const ownPostIds = ref(new Set())
  const ephemeralKeys = ref(new Map())

  // Notifications: [{ id, boardId, threadId, postId, refPostId, timestamp, read }]
  const notifications = ref([])

  const unreadCount = computed(() =>
    notifications.value.filter(n => !n.read).length
  )

  /**
   * Register a post we just created so we can detect replies to it.
   */
  function registerOwnPost(postId, privateKeyObj) {
    ownPostIds.value.add(postId)
    if (privateKeyObj) ephemeralKeys.value.set(postId, markRaw(privateKeyObj))
  }

  /**
   * Check a post's encrypted pings for references to our posts.
   */
  async function checkForMentions(post, _text, boardId) {
    if (!post?.post_id || ownPostIds.value.has(post.post_id)) return
    if (!Array.isArray(post?.encrypted_pings) || post.encrypted_pings.length === 0) return

    for (const ping of post.encrypted_pings) {
      if (typeof ping !== 'string' || !ping) continue

      for (const [refPostId, privateKeyObj] of ephemeralKeys.value.entries()) {
        const decryptedPostId = await decryptPing(privateKeyObj, ping)
        if (!decryptedPostId || decryptedPostId !== post.post_id) continue

        const exists = notifications.value.some(
          (n) => n.postId === post.post_id && n.refPostId === refPostId
        )
        if (!exists) {
          notifications.value.unshift({
            id: crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36),
            boardId,
            threadId: post.thread_id,
            postId: post.post_id,
            refPostId,
            timestamp: post.timestamp || Math.floor(Date.now() / 1000),
            read: false,
          })
        }
        break
      }
    }
  }

  function markRead(notifId) {
    const n = notifications.value.find(x => x.id === notifId)
    if (n) n.read = true
  }

  function markAllRead() {
    notifications.value.forEach(n => { n.read = true })
  }

  function clearAll() {
    notifications.value = []
  }

  return {
    ownPostIds, ephemeralKeys, notifications, unreadCount,
    registerOwnPost, checkForMentions,
    markRead, markAllRead, clearAll,
  }
})
