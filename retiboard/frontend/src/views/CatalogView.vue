<script setup>
import { ref, onMounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useBoardStore } from '../stores/boardStore.js'
import { useCacheStore } from '../stores/cacheStore.js'
import { useSettingsStore } from '../stores/settingsStore.js'
import { useAttachmentTracker } from '../stores/attachmentTracker.js'
import { useModerationStore } from '../stores/moderationStore.js'
import { useNotificationStore } from '../stores/notificationStore.js'
import { useBoardSocket } from '../composables/useSocket.js'
import { useDecrypt } from '../composables/useDecrypt.js'
import { useBannedItems } from '../composables/useBannedItems.js'
import { useBucketRoute } from '../composables/useBucketRoute.js'
import { useDelayedRefetch } from '../composables/useDelayedRefetch.js'
import { useCatalogThreads } from '../composables/useCatalogThreads.js'
import { useCatalogThreadContent } from '../composables/useCatalogThreadContent.js'
import { useCatalogModeration } from '../composables/useCatalogModeration.js'
import { timeAgo } from '../utils/time.js'
import PostComposer from '../components/PostComposer.vue'
import NotificationBell from '../components/NotificationBell.vue'
import CatalogThreadCard from '../components/CatalogThreadCard.vue'
import NetworkStatusIndicator from '../components/NetworkStatusIndicator.vue'
import BoardQuickNav from '../components/BoardQuickNav.vue'

const props = defineProps({ boardId: String })
const router = useRouter()
const route = useRoute()
const boardStore = useBoardStore()
const cacheStore = useCacheStore()
const settings = useSettingsStore()
const attachmentTracker = useAttachmentTracker()
const moderation = useModerationStore()
const notifStore = useNotificationStore()
const {
  getText,
  getAttachments,
  pauseAttachmentFetch,
  resumeAttachmentFetch,
  cancelAttachmentFetch,
  fetchPayloadProgress,
} = useDecrypt()
const { connected, onEvent } = useBoardSocket(props.boardId)

const showComposer = ref(false)
const {
  currentBucket,
  isHiddenBucket,
  isBannedBucket,
  queryForBucket,
  setBucket,
} = useBucketRoute({
  route,
  router,
  routeName: 'catalog',
  getParams: () => ({ boardId: props.boardId }),
})
const {
  loading,
  currentBoardStats,
  visibleThreads,
  availableComposerPosts,
  getLiveThread,
  removeThread,
  fetchCatalog,
  startStatusPolling,
} = useCatalogThreads({
  boardId: () => props.boardId,
  currentBucket,
  settings,
  moderation,
})
const {
  ensureThreadContent,
  getThreadContentState,
  evictCatalogThreadState,
  doLoadAttachments,
  retryAttachments,
  cancelAttachments,
  pauseThreadAttachments,
  resumeThreadAttachments,
} = useCatalogThreadContent({
  boardId: () => props.boardId,
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
})
const {
  scheduleRefetch: scheduleCatalogRefetch,
} = useDelayedRefetch({
  refetch: () => fetchCatalog(),
  shouldRetry: () => [...moderation.purgedThreadStubs.values()]
    .some((stub) => stub?.thread_id && !moderation.isThreadPurged(stub.thread_id)),
})
const {
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
} = useCatalogModeration({
  boardId: () => props.boardId,
  settings,
  moderation,
  scheduleCatalogRefetch,
  getLiveThread,
  removeThread,
  ensureThreadContent,
  evictCatalogThreadState,
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
  scheduleRefetch: scheduleCatalogRefetch,
})

// ── Misc helpers ───────────────────────────────────────────────────────────

function openThread(threadId) {
  router.push({
    name: 'thread',
    params: { boardId: props.boardId, threadId },
    query: queryForBucket(isHiddenBucket.value ? 'hidden' : 'main'),
  })
}
function onPostCreated() { showComposer.value = false; fetchCatalog() }

onEvent((msg) => {
  if (msg.event !== 'new_post') return
  notifStore.checkForMentions(msg.data, '', props.boardId)
  fetchCatalog(true)
})

onMounted(async () => {
  await boardStore.fetchBoards()
  boardStore.selectBoard(props.boardId)
  await moderation.hydrate(props.boardId)
  await startStatusPolling()
  await fetchCatalog()
})
</script>

<template>
  <div class="catalog">
    <header class="cat-header">
      <div class="cat-title">
        <h1 v-if="boardStore.currentBoard">◈/{{ boardStore.currentBoard.display_name }}/</h1>
      </div>
      <div class="cat-controls">
        <NetworkStatusIndicator />
        <span v-if="connected" class="live-dot" title="Live"></span>
        <button class="btn" @click="showComposer = !showComposer">
          {{ showComposer ? 'Close' : 'New Thread' }}
        </button>
        <button class="btn-dim" @click="fetchCatalog">↻</button>
        <NotificationBell />
      </div>
    </header>

    <BoardQuickNav :current-board-id="boardId" />

    <div class="bucket-tabs">
      <button class="btn-dim" :class="{ active: currentBucket === 'main' }" @click="setBucket('main')">Main</button>
      <button class="btn-dim" :class="{ active: currentBucket === 'hidden' }" @click="setBucket('hidden')">Hidden</button>
      <button class="btn-dim" :class="{ active: currentBucket === 'banned' }" @click="setBucket('banned')">Banned</button>
    </div>

    <PostComposer
      v-if="showComposer"
      :board-id="boardId"
      :is-op="true"
      :available-posts="availableComposerPosts"
      @posted="onPostCreated"
    />

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

    <div v-else-if="loading" class="loading">Loading catalog…</div>
    <div v-else-if="visibleThreads.length === 0" class="empty">
      {{ isHiddenBucket ? 'No hidden threads.' : 'No threads yet.' }}
    </div>

    <div v-else class="thread-grid">
      <CatalogThreadCard
        v-for="thread in visibleThreads"
        :key="thread.thread_id"
        :board-id="boardId"
        :thread="thread"
        :is-hidden-bucket="isHiddenBucket"
        :moderation="moderation"
        :settings="settings"
        :preview-text="getThreadContentState(thread).text"
        :attachment-state="getThreadContentState(thread)"
        :thread-reason="threadReason(thread)"
        :thread-hidden-reason="threadHiddenReason(thread)"
        :is-awaiting-network="isThreadAwaitingNetwork(thread)"
        @open-thread="openThread"
        @restore-thread="restoreThread"
        @hide-thread="hideThreadEntry"
        @purge-thread="purgeThreadEntry"
        @unpurge-thread="unpurgeThreadEntry"
        @pin-thread="pinThreadEntry"
        @unpin-thread="unpinThreadEntry"
        @hide-identity="hideIdentityEntry"
        @unhide-identity="unhideIdentityEntry"
        @ban-identity="banIdentityEntry"
        @load-attachments="doLoadAttachments($event, true)"
        @pause-attachments="async (hash, abort) => { await pauseAttachmentFetch(props.boardId, hash); await pauseThreadAttachments(hash); abort?.(); }"
        @resume-attachments="async (hash) => { await resumeAttachmentFetch(props.boardId, hash); await resumeThreadAttachments(hash); }"
        @retry-attachments="retryAttachments"
        @cancel-attachments="cancelAttachments"
      />
    </div>
  </div>
</template>

<style scoped>
.catalog { max-width: 960px; margin: 0 auto; padding: 1rem; }
.cat-header {
  display: flex; align-items: center; gap: 0.8rem;
  margin-bottom: 1rem; padding-bottom: 0.5rem; border-bottom: 1px solid #2a2a4a;
}
.back { color: #7070ff; text-decoration: none; font-size: 0.85rem; }
.cat-title { flex: 1; min-width: 0; }
.cat-header h1 { font-size: 1.1rem; font-weight: normal; color: #c0c0ff; }
.cat-peer-meta { color: #66667e; font-size: 0.72rem; margin-top: 0.15rem; }
.cat-controls { display: flex; align-items: center; gap: 0.4rem; }
.live-dot { width: 7px; height: 7px; border-radius: 50%; background: #40c040; }
.btn, .btn-dim, .btn-load {
  font-family: inherit; font-size: 0.78rem; padding: 0.3rem 0.7rem;
  border-radius: 3px; cursor: pointer; border: 1px solid #4040a0;
  background: #2a2a5a; color: #c0c0ff;
}
.btn:hover { background: #3a3a6a; }
.btn-dim { background: transparent; color: #606070; border-color: #303040; }
.btn-dim.danger { color: #d09090; border-color: #5a3030; }
.btn-load {
  font-family: inherit; font-size: 0.75rem; padding: 0.35rem 0.8rem;
  border-radius: 3px; cursor: pointer; border: 1px dashed #4040a0;
  background: #1a1a30; color: #8080c0;
}
.btn-load:hover { background: #2a2a4a; color: #c0c0ff; border-style: solid; }
.loading, .empty { color: #505060; text-align: center; padding: 3rem 0; font-size: 0.85rem; }

.bucket-tabs { display: flex; gap: 0.4rem; margin-bottom: 0.8rem; }
.btn-dim.active { color: #c0c0ff; border-color: #5050a0; background: #1a1a34; }

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
.ban-section { max-width: 600px; }

.thread-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 0.6rem;
}
</style>
