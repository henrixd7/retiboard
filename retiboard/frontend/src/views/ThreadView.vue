<script setup>
/**
 * Thread view — split-blob: text always loads, attachments on demand.
 * Attachments left / text right layout. Reply linking (>>hexid).
 *
 * Moderation UX:
 *   - Each post has a primary Reply button (↩) and a ⋮ menu for
 *     less-frequent actions: Hide post, Purge post.
 *   - Purge thread is in a ⋮ menu on the thread header, NOT a
 *     top-level button — it is a destructive action that should
 *     require deliberate navigation.
 *   - Hidden posts render as ModerationPlaceholder with "Show anyway"
 *     and "Restore" actions.
 *   - Purged posts ALSO render as ModerationPlaceholder (no silent
 *     disappearance) with a "Re-fetch from network" affordance so
 *     the user can recover if they purged by mistake.
 */
import { ref, onMounted, onUnmounted, nextTick, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useBoardStore } from '../stores/boardStore.js'
import { useSettingsStore } from '../stores/settingsStore.js'
import { useNotificationStore } from '../stores/notificationStore.js'
import { useCacheStore } from '../stores/cacheStore.js'
import { useAttachmentTracker } from '../stores/attachmentTracker.js'
import { useModerationStore } from '../stores/moderationStore.js'
import { useBoardSocket } from '../composables/useSocket.js'
import { useDecrypt } from '../composables/useDecrypt.js'
import { useBannedItems } from '../composables/useBannedItems.js'
import { useBucketRoute } from '../composables/useBucketRoute.js'
import { useDelayedRefetch } from '../composables/useDelayedRefetch.js'
import { useThreadVisiblePosts } from '../composables/useThreadVisiblePosts.js'
import { useThreadPostNavigation } from '../composables/useThreadPostNavigation.js'
import { useThreadPostAttachments } from '../composables/useThreadPostAttachments.js'
import PostComposer from '../components/PostComposer.vue'
import AttachmentOverlay from '../components/AttachmentOverlay.vue'
import PostPreview from '../components/PostPreview.vue'
import NotificationBell from '../components/NotificationBell.vue'
import ModerationPlaceholder from '../components/ModerationPlaceholder.vue'
import ThreadPostAttachments from '../components/ThreadPostAttachments.vue'
import ThreadPostText from '../components/ThreadPostText.vue'
import BoardQuickNav from '../components/BoardQuickNav.vue'
import { timeAgo } from '../utils/time.js'
import { ApiError, apiJson } from '../utils/api.js'

const TEXT_TRUNCATE_LIMIT = 2000
const TEXT_RETRY_DELAY_MS = 2500
const MAX_TEXT_RETRY_ATTEMPTS = 6

const props = defineProps({ boardId: String, threadId: String })
const router = useRouter()
const route = useRoute()
const boardStore = useBoardStore()
const settings = useSettingsStore()
const notifStore = useNotificationStore()
const cacheStore = useCacheStore()
const attachmentTracker = useAttachmentTracker()
const moderation = useModerationStore()
const {
  getText,
  getAttachments,
  pauseAttachmentFetch,
  resumeAttachmentFetch,
  cancelAttachmentFetch,
  fetchPayloadProgress,
} = useDecrypt()
const { connected, onEvent } = useBoardSocket(props.boardId)

const posts = ref([])
const postText = ref({})
const loading = ref(true)
const showReply = ref(false)
const moderationBusy = ref(false)
const composerRef = ref(null)
const replyComposerAnchor = ref(null)
const textLoadInFlight = new Set()
const textRetryTimers = new Map()
const textRetryAttempts = new Map()

const {
  currentBucket,
  currentBucketQuery,
  isHiddenBucket,
  isBannedBucket,
  setBucket,
} = useBucketRoute({
  route,
  router,
  routeName: 'thread',
  getParams: () => ({
    boardId: props.boardId,
    threadId: props.threadId,
  }),
})
const {
  scheduleRefetch: scheduleThreadRefetch,
} = useDelayedRefetch({
  refetch: () => fetchThread(),
  shouldRetry: () => [...moderation.purgedPostStubs.values()]
    .some((stub) => stub?.thread_id === props.threadId && !moderation.isPostPurged(stub.post_id)),
})
const {
  bannedItems,
  bannedFileItems,
  unbanIdentityItem,
  downgradeToHide,
  unbanLocalFileHash,
} = useBannedItems({
  boardId: () => props.boardId,
  isActive: isBannedBucket,
  moderation,
  cacheStore,
  moderationBusy,
  scheduleRefetch: scheduleThreadRefetch,
})

function postHiddenReason(post) {
  return moderation.getPostHiddenReason(post)
}

function postBucket(post) {
  return moderation.getPostBucket(post)
}

/**
 * A post is "purged" when its data has been hard-deleted locally.
 * Purged posts still render via session-local structural stubs until the
 * tombstone is lifted or the network re-gossips the post.
 */
function isPostPurged(post) {
  return moderation.isPostPurged(post.post_id) || moderation.isThreadPurged(post.thread_id)
}

const {
  visiblePosts,
  dropReconciledPurgedPostStubs,
} = useThreadVisiblePosts({
  posts,
  threadId: () => props.threadId,
  currentBucket,
  postBucket,
  moderation,
})
const {
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
} = useThreadPostAttachments({
  boardId: () => props.boardId,
  settings,
  moderation,
  cacheStore,
  attachmentTracker,
  getAttachments,
  fetchPayloadProgress,
  pauseAttachmentFetch,
  resumeAttachmentFetch,
  cancelAttachmentFetch,
})
const {
  hoverPreview,
  scrollEdgeDir,
  scrollToPost,
  scrollToEdge,
  showPostPreview,
  hidePostPreview,
  clearPreviewForPost,
} = useThreadPostNavigation({
  route,
  router,
  posts,
  postText,
})

// Only one menu open at a time; stores post_id or 'thread' for thread menu.
const openMenuId = ref(null)

function toggleMenu(id) {
  openMenuId.value = openMenuId.value === id ? null : id
}

function closeMenu() {
  openMenuId.value = null
}

function onDocClick(event) {
  if (!event.target.closest('.dot-menu-wrap')) closeMenu()
}

function getLivePost(postId) {
  return posts.value.find((post) => post.post_id === postId) || null
}

function clearTextRetry(postId) {
  const timer = textRetryTimers.get(postId)
  if (timer) clearTimeout(timer)
  textRetryTimers.delete(postId)
  textRetryAttempts.delete(postId)
}

function scheduleTextRetry(post) {
  const postId = post?.post_id
  if (!postId || textRetryTimers.has(postId)) return

  const attempt = (textRetryAttempts.get(postId) || 0) + 1
  if (attempt > MAX_TEXT_RETRY_ATTEMPTS) {
    postText.value[postId] = null
    clearTextRetry(postId)
    return
  }

  textRetryAttempts.set(postId, attempt)
  const timer = window.setTimeout(() => {
    textRetryTimers.delete(postId)
    const livePost = getLivePost(postId)
    if (!livePost) return
    void loadText(livePost, { force: true })
  }, TEXT_RETRY_DELAY_MS)
  textRetryTimers.set(postId, timer)
}

function evictThreadPostState(postId) {
  const livePost = getLivePost(postId)
  if (livePost) {
    evictThreadPostAttachments(livePost)
    if (livePost.content_hash) cacheStore.remove(`text:${livePost.content_hash}`)
  }

  delete postText.value[postId]
  clearTextRetry(postId)
  clearPreviewForPost(postId)
  posts.value = posts.value.filter((post) => post.post_id !== postId)
}

function evictVisibleThreadState() {
  for (const post of posts.value) {
    evictThreadPostAttachments(post)
    if (post.content_hash) cacheStore.remove(`text:${post.content_hash}`)
    delete postText.value[post.post_id]
    clearTextRetry(post.post_id)
  }

  hidePostPreview()
  clearAllAttachmentState()
  posts.value = []
}

async function showPostAnyway(post) {
  moderation.allowPostThisSession(post.post_id)
  void loadText(post)
  void maybeLoadAttachments(post)
}

async function restorePost(post) {
  const reason = postHiddenReason(post)
  moderationBusy.value = true
  try {
    if (reason === 'hidden_post') {
      await moderation.unhidePost(props.boardId, post.post_id)
    } else if (reason === 'hidden_thread') {
      await moderation.unhideThread(props.boardId, post.thread_id)
    } else if (reason === 'hidden_identity') {
      const identityHash = post.identity_hash || ''
      if (identityHash) await moderation.unhideIdentity(props.boardId, identityHash)
    }

    void loadText(post)
    void maybeLoadAttachments(post)
  } finally {
    moderationBusy.value = false
  }
}

async function hidePostEntry(post) {
  closeMenu()
  moderationBusy.value = true
  try {
    await moderation.hidePost(props.boardId, post.post_id)
  } finally {
    moderationBusy.value = false
  }
}

async function hideThreadFromHeader() {
  closeMenu()
  moderationBusy.value = true
  try {
    await moderation.hideThread(props.boardId, props.threadId)
  } finally {
    moderationBusy.value = false
  }
}

async function purgePostEntry(post) {
  if (post.post_id === post.thread_id) {
    return purgeThreadEntry()
  }

  closeMenu()
  moderationBusy.value = true
  try {
    const stub = {
      post_id: post.post_id,
      thread_id: post.thread_id,
      identity_hash: post.identity_hash || '',
      timestamp: post.timestamp || 0,
      parent_id: post.parent_id || null,
    }
    await moderation.purgePost(props.boardId, post.post_id, stub)
    evictThreadPostState(post.post_id)
  } finally {
    moderationBusy.value = false
  }
}

async function purgeThreadEntry() {
  if (settings.isThreadPinned(props.boardId, props.threadId)) {
    window.alert('Unpin this thread before purging it.')
    return
  }

  closeMenu()
  moderationBusy.value = true
  try {
    await moderation.purgeThread(props.boardId, props.threadId, {
      thread_id: props.threadId,
      identity_hash: posts.value[0]?.identity_hash || '',
      thread_last_activity: posts.value[posts.value.length - 1]?.created_at
        || Math.floor(Date.now() / 1000),
    })
    evictVisibleThreadState()
    router.push({
      name: 'catalog',
      params: { boardId: props.boardId },
      query: currentBucketQuery.value,
    })
  } finally {
    moderationBusy.value = false
  }
}

async function redownloadPost(post) {
  moderationBusy.value = true
  try {
    evictThreadPostState(post.post_id)
    await moderation.unpurgePost(props.boardId, post.post_id)
    scheduleThreadRefetch(0)
  } finally {
    moderationBusy.value = false
  }
}

async function hideIdentityEntry(post) {
  closeMenu()
  const identityHash = post.identity_hash || ''
  if (!identityHash) return

  moderationBusy.value = true
  try {
    await moderation.hideIdentity(props.boardId, identityHash)
  } finally {
    moderationBusy.value = false
  }
}

async function unhideIdentityEntry(post) {
  closeMenu()
  const identityHash = post.identity_hash || ''
  if (!identityHash) return

  moderationBusy.value = true
  try {
    await moderation.unhideIdentity(props.boardId, identityHash)
  } finally {
    moderationBusy.value = false
  }
}

async function banIdentityEntry(post) {
  closeMenu()
  const identityHash = post.identity_hash || ''
  if (!identityHash) return

  moderationBusy.value = true
  try {
    const result = await moderation.banIdentity(props.boardId, identityHash)
    if (result?.purged_post_ids) {
      for (const postId of result.purged_post_ids) {
        evictThreadPostState(postId)
      }
    }
  } finally {
    moderationBusy.value = false
  }
}

async function fetchThread(silent = false) {
  if (!silent) loading.value = true

  try {
    const fetched = await apiJson(`/api/boards/${props.boardId}/threads/${props.threadId}`)
    posts.value = fetched

    for (const post of fetched) {
      if (post?._isStub || isPostPurged(post)) continue
      await notifStore.checkForMentions(post, '', props.boardId)
    }

    dropReconciledPurgedPostStubs(fetched)

    for (const post of posts.value) {
      if (isPostPurged(post)) continue
      if (postBucket(post) !== currentBucket.value) continue
      void loadText(post)
      void maybeLoadAttachments(post)
    }
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      router.push({ name: 'catalog', params: { boardId: props.boardId } })
      return
    }
    console.error('Thread fetch failed:', error)
  } finally {
    loading.value = false
  }
}

async function loadText(post, options = {}) {
  const postId = post?.post_id
  if (!postId) return
  if (textLoadInFlight.has(postId)) return
  if (!options.force && postText.value[postId] !== undefined) return

  textLoadInFlight.add(postId)
  try {
    const result = await getText(props.boardId, post.content_hash, post.attachment_content_hash)
    if (result?.retryable && typeof result?.text !== 'string') {
      delete postText.value[postId]
      scheduleTextRetry(post)
      return
    }

    clearTextRetry(postId)
    postText.value[postId] = result?.text ?? null

    if (result?.attachments && !post.attachment_content_hash) {
      setInlineAttachments(post, result.attachments)
    }

    await notifStore.checkForMentions(post, result?.text ?? '', props.boardId)
  } finally {
    textLoadInFlight.delete(postId)
  }
}

function formatTime(timestamp) {
  return new Date(timestamp * 1000).toLocaleString()
}

function scrollReplyComposerIntoView() {
  nextTick(() => {
    window.setTimeout(() => {
      replyComposerAnchor.value?.scrollIntoView({
        behavior: 'smooth',
        block: 'start',
      })
    }, 40)
  })
}

function replyToPost(postId) {
  showReply.value = true
  nextTick(() => {
    if (composerRef.value) composerRef.value.quotePost(postId)
  })
  scrollReplyComposerIntoView()
}

function onReplyPosted() {
  showReply.value = false
  void fetchThread(true)
}

watch(showReply, (isVisible) => {
  if (!isVisible) return
  scrollReplyComposerIntoView()
})

onEvent((msg) => {
  if (msg.event !== 'new_post') return

  void notifStore.checkForMentions(msg.data, '', props.boardId)
  if (msg.data?.thread_id === props.threadId) {
    void fetchThread(true)
  }
})

watch(
  visiblePosts,
  (items) => {
    for (const post of items) {
      if (post?._isStub || isPostPurged(post)) continue
      void loadText(post)
      void maybeLoadAttachments(post)
    }
  },
  { immediate: true },
)

onMounted(async () => {
  document.addEventListener('click', onDocClick)
  await boardStore.fetchBoards()
  boardStore.selectBoard(props.boardId)
  await moderation.hydrate(props.boardId)
  await fetchThread()
})

onUnmounted(() => {
  document.removeEventListener('click', onDocClick)
  for (const timer of textRetryTimers.values()) {
    clearTimeout(timer)
  }
  textRetryTimers.clear()
  textRetryAttempts.clear()
})
</script>

<template>
  <div class="thread-view">
    <!-- ── Thread header ─────────────────────────────────────────────── -->
    <header class="tv-header">
      <router-link :to="{ name: 'catalog', params: { boardId }, query: currentBucketQuery }" class="back">← catalog</router-link>
      <h1>Thread {{ threadId.substring(0, 12) }}…</h1>

      <!-- Thread-level dot-menu — houses the dangerous "Purge thread" action -->
      <div class="dot-menu-wrap">
        <button
          class="dot-menu-btn"
          :class="{ active: openMenuId === 'thread' }"
          title="Thread options"
          @click.stop="toggleMenu('thread')"
        >⋮</button>
        <div v-if="openMenuId === 'thread'" class="dot-menu">
          <button
            v-if="settings.isThreadPinned(boardId, threadId)"
            class="dm-item"
            @click="settings.unpinThread(boardId, threadId); closeMenu()"
          >Unpin thread</button>
          <button
            v-else
            class="dm-item"
            @click="settings.pinThread(boardId, threadId); closeMenu()"
          >Pin thread</button>
          <div class="dm-sep"></div>
          <button v-if="!isHiddenBucket" class="dm-item" @click="hideThreadFromHeader">
            Hide thread
          </button>
          <div class="dm-sep"></div>
          <button class="dm-item dm-danger" @click="purgeThreadEntry">
            Purge thread locally
          </button>
          <template v-if="posts[0]?.identity_hash">
            <div class="dm-sep"></div>
            <button v-if="!isHiddenBucket" class="dm-item" @click="hideIdentityEntry(posts[0])">Hide identity</button>
            <button class="dm-item dm-danger" @click="banIdentityEntry(posts[0])">Ban identity</button>
          </template>
        </div>
      </div>

      <span v-if="connected" class="live-dot" title="Live"></span>
      <NotificationBell />
    </header>

    <BoardQuickNav :current-board-id="boardId" />

    <div class="bucket-tabs">
      <button
        class="btn-dim"
        :class="{ active: currentBucket === 'main' }"
        @click="setBucket('main')"
      >Main</button>
      <button
        class="btn-dim"
        :class="{ active: currentBucket === 'hidden' }"
        @click="setBucket('hidden')"
      >Hidden</button>
      <button
        class="btn-dim"
        :class="{ active: currentBucket === 'banned' }"
        @click="setBucket('banned')"
      >Banned</button>
    </div>

    <!-- ── Banned bucket: identity, legacy attachment, and local file-ban management ─────────── -->
    <div v-if="isBannedBucket" class="ban-section">
      <h3 class="ban-heading">Banned Identities</h3>
      <div v-if="bannedItems.identities.length === 0" class="empty">No banned identities.</div>
      <div v-else class="ban-list">
        <div v-for="item in bannedItems.identities" :key="item.target_id" class="ban-card">
          <div class="ban-top">
            <span class="ban-id">ID:{{ item.target_id.substring(0, 12) }}…</span>
            <span class="ban-meta">{{ timeAgo(item.created_at) }} ago</span>
          </div>
          <div v-if="item.reason" class="ban-reason">{{ item.reason }}</div>
          <div class="ban-actions">
            <button class="btn-dim" :disabled="moderationBusy" @click="unbanIdentityItem(item)">Unban</button>
            <button class="btn-dim" :disabled="moderationBusy" @click="downgradeToHide(item)">Change to hide</button>
          </div>
        </div>
      </div>

      <!--
      <h3 class="ban-heading">Banned Attachments</h3>
      <div v-if="bannedItems.attachments.length === 0" class="empty">No banned attachments.</div>
      <div v-else class="ban-list">
        <div v-for="item in bannedItems.attachments" :key="item.target_id" class="ban-card">
          <div class="ban-top">
            <span class="ban-id">{{ item.target_id.substring(0, 16) }}…</span>
            <span class="ban-meta">{{ timeAgo(item.created_at) }} ago</span>
          </div>
          <div v-if="item.reason" class="ban-reason">{{ item.reason }}</div>
          <div class="ban-actions">
            <button class="btn-dim" :disabled="moderationBusy" @click="unbanAttachmentItem(item)">Unban</button>
          </div>
        </div>
      </div>
      -->

      <h3 class="ban-heading">Banned Files</h3>
      <div v-if="bannedFileItems.length === 0" class="empty">No banned local files.</div>
      <div v-else class="ban-list">
        <div v-for="fileHash in bannedFileItems" :key="fileHash" class="ban-card">
          <div class="ban-top">
            <span class="ban-id">{{ fileHash.substring(0, 16) }}…</span>
            <span class="ban-meta">local file hash</span>
          </div>
          <div class="ban-actions">
            <button class="btn-dim" :disabled="moderationBusy" @click="unbanLocalFileHash(fileHash)">Unban</button>
          </div>
        </div>
      </div>
    </div>

    <div v-else-if="loading" class="loading">Loading thread…</div>

    <div v-else class="posts">
      <template v-for="(post, idx) in visiblePosts" :key="post.post_id">

        <!-- ── Purged post: tombstone still active, data gone ──────────── -->
        <!--
          isPostPurged() checks the purgedPosts Set AND purgedThreads Set.
          Only _isStub objects (injected by visiblePosts computed) reach this
          branch — live API post objects are never flagged purged here because
          the receiver rejects re-admission until the tombstone is lifted.
        -->
        <ModerationPlaceholder
          v-if="isPostPurged(post)"
	  :id="`post-${post.post_id}`"
          kind="post"
          :reason="moderation.isThreadPurged(post.thread_id) ? 'purged_thread' : 'purged_post'"
          :primary-id="post.post_id.substring(0, 12)"
          :secondary-text="post.identity_hash ? `ID:${post.identity_hash.substring(0, 8)}` : ''"
          :can-show="false"
          :can-hide="!isHiddenBucket"
          hide-label="Hide post"
          :can-restore="isHiddenBucket"
          restore-label="Unhide post"
          :can-purge="false"
          :can-redownload="!moderation.isThreadPurged(post.thread_id)"
          @hide="hidePostEntry(post)"
          @restore="restorePost(post)"
          redownload-label="Undo purge — re-fetch from network"
          @redownload="redownloadPost(post)"
        />

        <!-- ── Purged post: tombstone lifted, awaiting gossip ──────────── -->
        <!--
          IMPORTANT: gated on post._isStub — only stub objects injected by
          visiblePosts can enter this branch.  A live API post object never
          has _isStub: true, so even if the store's purgedPostStubs Map still
          contains a matching entry (async cleanup not yet run), the live post
          will fall through to the normal visible-post branch below.
          This is the dedup invariant that eliminates the "Awaiting sync" flash.
        -->
        <ModerationPlaceholder
          v-else-if="post._isStub && moderation.getPurgedPostStub(post.post_id)"
	  :id="`post-${post.post_id}`"
          kind="post"
          reason="awaiting_network"
          :primary-id="post.post_id.substring(0, 12)"
          secondary-text="Purge undone — waiting for network sync…"
          :can-show="false"
          :can-restore="false"
          :can-purge="false"
          :can-redownload="false"
        />

        <!-- ── Visible post ─────────────────────────────────────────── -->
        <article
          v-else
          :id="`post-${post.post_id}`"
          :class="['post', { op: idx === 0, hidden: isHiddenBucket }]"
        >
          <div class="post-header">
            <span class="post-num">#{{ idx + 1 }}</span>
            <span class="post-id" :title="post.post_id">{{ post.post_id.substring(0, 10) }}</span>
            <span class="post-time">{{ formatTime(post.timestamp) }}</span>
            <span v-if="post.identity_hash" class="post-identity">
              ID:{{ post.identity_hash.substring(0, 8) }}
            </span>
            <span v-if="!post.bump_flag" class="sage">sage</span>
            <span v-if="isHiddenBucket" class="hidden-badge">
              {{ postHiddenReason(post) || 'hidden' }}
            </span>

            <!-- Post actions: reply is primary, destructive behind ⋮ -->
            <span class="post-actions">
              <button class="act act-reply" title="Reply" @click.stop="replyToPost(post.post_id)">
                ↩ Reply
              </button>
              <div class="dot-menu-wrap">
                <button
                  class="act act-menu"
                  :class="{ active: openMenuId === post.post_id }"
                  title="Post options"
                  @click.stop="toggleMenu(post.post_id)"
                >⋮</button>
                <div v-if="openMenuId === post.post_id" class="dot-menu dot-menu-left">
                  <button v-if="!isHiddenBucket" class="dm-item" @click="hidePostEntry(post)">Hide post</button>
                  <button v-else class="dm-item" @click="restorePost(post)">Unhide</button>
                  <div class="dm-sep"></div>
                  <button class="dm-item dm-danger" @click="purgePostEntry(post)">
                    Purge post locally
                  </button>
                  <template v-if="post.identity_hash">
                    <div class="dm-sep"></div>
                    <button v-if="isHiddenBucket && postHiddenReason(post) === 'hidden_identity'" class="dm-item" @click="unhideIdentityEntry(post)">Unhide identity</button>
                    <button v-if="!isHiddenBucket" class="dm-item" @click="hideIdentityEntry(post)">Hide identity</button>
                    <button class="dm-item dm-danger" @click="banIdentityEntry(post)">Ban identity</button>
                  </template>
                </div>
              </div>
            </span>
          </div>

          <!-- Content: attachments left, text right -->
          <div class="post-content">
            <ThreadPostAttachments
              :post="post"
              :state="getPostAttachmentState(post)"
              :attachment-banned="Boolean(
                post.attachment_content_hash
                && moderation.isAttachmentBanned(post.attachment_content_hash)
              )"
              @open="openOverlay"
              @ban-file="banFileLocally"
              @unban-file="unbanLocalFileFromPost"
              @hide-placeholder="hideBannedFilePlaceholder"
              @pause="pausePostAttachments"
              @resume="resumePostAttachments"
              @retry="retryAttachments"
              @cancel="cancelAttachments"
              @load="doLoadAttachments($event, true)"
            />

            <template v-if="postText[post.post_id] !== undefined">
              <span v-if="postText[post.post_id] === null" class="unavailable">Payload unavailable</span>
              <ThreadPostText
                v-else
                :text="postText[post.post_id]"
                :posts="posts"
                :truncate-limit="TEXT_TRUNCATE_LIMIT"
                @scroll-to-post="scrollToPost"
                @show-post-preview="showPostPreview"
                @hide-post-preview="hidePostPreview"
              />
            </template>
            <span v-else class="decrypting">Decrypting…</span>
          </div>
        </article>

      </template>
    </div>

    <div class="reply-section">
      <button class="btn" @click="showReply = !showReply">
        {{ showReply ? 'Close' : 'Reply' }}
      </button>
    </div>
    <div v-if="showReply" ref="replyComposerAnchor" class="reply-composer-anchor">
      <PostComposer
        ref="composerRef"
        :board-id="boardId"
        :thread-id="threadId"
        :is-op="false"
        :available-posts="visiblePosts"
        @posted="onReplyPosted"
      />
    </div>

    <AttachmentOverlay
      v-if="overlayAttachment"
      :src="overlayAttachment.src"
      :mime-type="overlayAttachment.mimeType"
      @close="closeOverlay"
    />

    <Teleport to="body">
      <PostPreview
        v-if="hoverPreview"
        :post="hoverPreview.post"
        :text="hoverPreview.text"
        :anchor-el="hoverPreview.anchorEl"
      />
    </Teleport>

    <button
      class="scroll-edge-btn"
      :title="scrollEdgeDir === 'down' ? 'Jump to bottom' : 'Jump to top'"
      @click="scrollToEdge"
    >{{ scrollEdgeDir === 'down' ? '↓' : '↑' }}</button>
  </div>
</template>

<style scoped>
.thread-view { max-width: 860px; margin: 0 auto; padding: 1rem 1rem 72px; }

.tv-header {
  display: flex; align-items: center; gap: 0.6rem;
  margin-bottom: 1rem; padding-bottom: 0.5rem; border-bottom: 1px solid #2a2a4a;
}
.back { color: #7070ff; text-decoration: none; font-size: 0.8rem; }
.tv-header h1 { flex: 1; font-size: 1rem; font-weight: normal; color: #c0c0ff; }
.live-dot { width: 7px; height: 7px; border-radius: 50%; background: #40c040; }
.loading { color: #505060; text-align: center; padding: 3rem 0; }
.bucket-tabs { display: flex; gap: 0.4rem; margin-bottom: 0.8rem; }

/* ── Ban section ───────────────────────────────────────────────────────── */
.ban-section { max-width: 600px; }
.ban-list { display: grid; gap: 0.7rem; }
.ban-card {
  background: #161630; border: 1px solid #3a2a48; border-radius: 4px;
  padding: 0.8rem;
}
.ban-top { display: flex; justify-content: space-between; gap: 1rem; align-items: center; }
.ban-id { font-family: monospace; color: #c0c0ff; font-size: 0.85rem; }
.ban-meta, .ban-reason { color: #70708a; font-size: 0.75rem; }
.ban-reason { margin-top: 0.45rem; }
.ban-heading { color: #9090c0; font-size: 0.9rem; margin: 1.2rem 0 0.5rem; font-weight: 600; }
.ban-heading:first-child { margin-top: 0; }
.ban-actions { display: flex; gap: 0.4rem; margin-top: 0.5rem; }
.empty { color: #505060; text-align: center; padding: 1rem 0; font-size: 0.85rem; }

/* ── Dot-menu ──────────────────────────────────────────────────────────── */
.dot-menu-wrap { position: relative; }

.dot-menu-btn {
  background: none; border: 1px solid transparent; color: #5a5a7a;
  font-size: 1rem; line-height: 1; padding: 0.1rem 0.35rem; border-radius: 3px;
  cursor: pointer; transition: color 0.15s, border-color 0.15s;
}
.dot-menu-btn:hover,
.dot-menu-btn.active { color: #9090c0; border-color: #3a3a5a; background: #1a1a30; }

.dot-menu {
  position: absolute; top: calc(100% + 4px); right: 0; z-index: 200;
  background: #1a1a32; border: 1px solid #3a3a60; border-radius: 4px;
  min-width: 180px; padding: 0.25rem 0; box-shadow: 0 4px 16px rgba(0,0,0,0.5);
}
.dot-menu-left { right: 0; left: auto; }

.dm-item {
  display: block; width: 100%; text-align: left;
  background: none; border: none; padding: 0.4rem 0.8rem;
  font-family: inherit; font-size: 0.78rem; color: #a0a0c0; cursor: pointer;
}
.dm-item:hover { background: #252545; color: #d0d0ff; }
.dm-item:disabled { opacity: 0.55; cursor: default; }
.dm-item:disabled:hover { background: none; color: #a0a0c0; }
.dm-item.dm-danger { color: #b07070; }
.dm-item.dm-danger:hover { background: #2a1818; color: #d09090; }

.dm-sep { height: 1px; background: #2a2a48; margin: 0.25rem 0; }

/* ── Posts list ────────────────────────────────────────────────────────── */
.posts { display: flex; flex-direction: column; gap: 0.5rem; }
.post {
  background: #161630; border: 1px solid #2a2a4a; border-radius: 3px;
  padding: 0.6rem 0.8rem; transition: background 0.3s;
}
.post.hidden { border-color: #4a4440; background: #191720; }
.post.op { border-color: #3a3a6a; }
.post.highlight { background: #1e1e40; border-color: #6060c0; }

/* ── Post header ───────────────────────────────────────────────────────── */
.post-header {
  display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap;
  font-size: 0.7rem; color: #606080; margin-bottom: 0.4rem;
}
.post-num { color: #8080c0; font-weight: bold; }
.post-id { font-family: monospace; color: #505060; cursor: help; }
.post-time { color: #505060; }
.post-identity { color: #a0a060; }
.sage { color: #c06060; font-style: italic; }
.hidden-badge {
  font-size: 0.62rem;
  padding: 0.08rem 0.28rem;
  border-radius: 999px;
  background: #2b241e;
  color: #d0b090;
}

/* ── Post action buttons ───────────────────────────────────────────────── */
.post-actions { margin-left: auto; display: flex; align-items: center; gap: 0.3rem; }

/* Reply is the primary action — slightly more visible */
.act-reply {
  background: none; border: 1px solid #38386a; border-radius: 3px;
  color: #8888c8; cursor: pointer; font-family: inherit;
  font-size: 0.72rem; padding: 0.18rem 0.45rem;
  transition: color 0.15s, border-color 0.15s, background 0.15s;
}
.act-reply:hover { color: #c0c0ff; border-color: #6060a0; background: #1e1e3a; }

/* ⋮ menu toggle — understated */
.act { background: none; border: none; color: #505060; cursor: pointer; font-size: 0.75rem; padding: 0 0.25rem; }
.act:hover { color: #a0a0ff; }
.act-menu {
  background: none; border: 1px solid transparent; color: #505070;
  font-size: 1rem; line-height: 1; padding: 0.1rem 0.3rem; border-radius: 3px; cursor: pointer;
}
.act-menu:hover, .act-menu.active { color: #9090c0; border-color: #3a3a5a; background: #1a1a30; }

/* ── Post content ──────────────────────────────────────────────────────── */
.post-content { display: flex; gap: 0.8rem; align-items: flex-start; }

.post-attachments {
  flex-shrink: 0; display: flex; flex-direction: column; gap: 0.4rem; max-width: 250px;
}

.att-banned-placeholder {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  padding: 0.4rem 0.6rem;
  border: 1px dashed #5a3040;
  border-radius: 4px;
  background: #160f14;
}
.att-banned-label {
  font-size: 0.7rem;
  color: #c8a0a0;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.att-warning-placeholder {
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  max-width: 250px;
  padding: 0.55rem 0.7rem;
  border: 1px dashed #5b435e;
  border-radius: 4px;
  background: #16111d;
}

.att-warning-title {
  font-size: 0.72rem;
  font-weight: 700;
  color: #d8c0da;
}

.att-warning-meta {
  font-size: 0.68rem;
  line-height: 1.35;
  color: #9885a0;
}

.post-attachments-deferred { flex-shrink: 0; }
.post-attachments-deferred.loading { min-width: 220px; }
.btn-load {
  font-family: inherit; font-size: 0.75rem; padding: 0.35rem 0.8rem;
  border-radius: 3px; cursor: pointer; border: 1px dashed #4040a0;
  background: #1a1a30; color: #8080c0;
}
.btn-load:hover { background: #2a2a4a; color: #c0c0ff; border-style: solid; }

.post-text {
  flex: 1; font-size: 0.85rem; line-height: 1.55;
  color: #c0c0d0; white-space: pre-wrap; word-break: break-word;
}
.post-ref { color: #7070ff; cursor: pointer; text-decoration: none; font-family: monospace; font-size: 0.8rem; }
.post-ref:hover { color: #a0a0ff; }
.post-ref.dead { color: #505060; cursor: default; text-decoration: line-through; }
.post-link { color: #60a0ff; text-decoration: none; word-break: break-all; font-size: 0.8rem; }
.post-link:hover { color: #90c0ff; }
.greentext { color: #02af02; }
.bluetext { color: #3381d2; }
.unavailable { color: #806060; font-style: italic; font-size: 0.8rem; }
.decrypting { color: #606060; font-style: italic; font-size: 0.8rem; }
.truncation { color: #606080; }
.expand-btn {
  background: none; border: none; color: #6060c0; cursor: pointer;
  font-family: inherit; font-size: 0.75rem; padding: 0.1rem 0.3rem; text-decoration: underline;
}
.expand-btn:hover { color: #a0a0ff; }

.reply-section { margin-top: 1rem; }
.reply-composer-anchor { margin-bottom: 0.5rem; }
.btn {
  font-family: inherit; font-size: 0.78rem; padding: 0.3rem 0.7rem;
  border-radius: 3px; cursor: pointer; border: 1px solid #4040a0;
  background: #2a2a5a; color: #c0c0ff;
}
.btn:hover { background: #3a3a6a; }
.btn-dim {
  font-family: inherit; font-size: 0.76rem; padding: 0.3rem 0.6rem;
  border-radius: 3px; cursor: pointer; border: 1px solid #303040; background: transparent; color: #9090b0;
}
.btn-dim:hover { background: #222238; }
.btn-dim.active { color: #c0c0ff; border-color: #5050a0; background: #1a1a34; }
.btn-dim.danger { color: #d09090; border-color: #5a3030; }

.attachment-progress-actions { display: flex; gap: 0.5rem; margin-top: 0.5rem; }

@media (max-width: 500px) {
  .post-content { flex-direction: column; }
  .post-attachments { max-width: 100%; }
}

/* ── Scroll edge button ──────────────────────────────────────────────────── */
.scroll-edge-btn {
  position: fixed;
  right: 1.2rem;
  bottom: 1.5rem;
  z-index: 200;
  width: 2.2rem;
  height: 2.2rem;
  background: #14142e;
  border: 1px solid #3a3a6a;
  border-radius: 50%;
  color: #7070b0;
  font-size: 1.1rem;
  line-height: 1;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  opacity: 0.75;
  transition: opacity 0.15s, border-color 0.15s, color 0.15s;
}
.scroll-edge-btn:hover {
  opacity: 1;
  border-color: #6060b0;
  color: #c0c0ff;
}
</style>
