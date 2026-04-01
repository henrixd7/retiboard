<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import ModerationPlaceholder from './ModerationPlaceholder.vue'
import AttachmentFetchProgress from './AttachmentFetchProgress.vue'
import AttachmentBundle from './AttachmentBundle.vue'
import { timeAgo } from '../utils/time.js'

const MENU_OPEN_EVENT = 'retiboard:catalog-menu-open'

const props = defineProps({
  boardId: {
    type: String,
    required: true,
  },
  thread: {
    type: Object,
    required: true,
  },
  isHiddenBucket: {
    type: Boolean,
    default: false,
  },
  moderation: {
    type: Object,
    required: true,
  },
  settings: {
    type: Object,
    required: true,
  },
  previewText: {
    type: String,
    default: '',
  },
  attachmentState: {
    type: Object,
    required: true,
  },
  threadReason: {
    type: String,
    default: '',
  },
  threadHiddenReason: {
    type: String,
    default: '',
  },
  isAwaitingNetwork: {
    type: Boolean,
    default: false,
  },
})

const emit = defineEmits([
  'open-thread',
  'restore-thread',
  'hide-thread',
  'purge-thread',
  'unpurge-thread',
  'pin-thread',
  'unpin-thread',
  'hide-identity',
  'unhide-identity',
  'ban-identity',
  'load-attachments',
  'pause-attachments',
  'resume-attachments',
  'retry-attachments',
  'cancel-attachments',
])

const openMenu = ref(false)
const hasIdentity = computed(() => Boolean(props.thread.identity_hash || props.thread.op_identity_hash))
const isPinned = computed(() => {
  return props.settings.isThreadPinned(props.boardId, props.thread.thread_id)
})
const isPurgedStub = computed(() => {
  return props.thread._isStub && props.moderation.isThreadPurged(props.thread.thread_id)
})

function truncText(text, max = 180) {
  if (!text) return ''
  return text.length <= max ? text : `${text.substring(0, max)}…`
}

function closeMenu() {
  openMenu.value = false
}

function toggleMenu() {
  const nextState = !openMenu.value
  if (nextState) {
    window.dispatchEvent(new CustomEvent(MENU_OPEN_EVENT, {
      detail: { threadId: props.thread.thread_id },
    }))
  }
  openMenu.value = nextState
}

function onDocClick(event) {
  if (!event.target.closest('.tc-menu-wrap')) closeMenu()
}

function onPeerMenuOpen(event) {
  if (event.detail?.threadId === props.thread.thread_id) return
  closeMenu()
}

function emitAction(name) {
  closeMenu()
  emit(name, props.thread)
}

watch(openMenu, (isOpen) => {
  if (isOpen) {
    document.addEventListener('click', onDocClick)
    return
  }
  document.removeEventListener('click', onDocClick)
})

onUnmounted(() => {
  document.removeEventListener('click', onDocClick)
  window.removeEventListener(MENU_OPEN_EVENT, onPeerMenuOpen)
})

onMounted(() => {
  window.addEventListener(MENU_OPEN_EVENT, onPeerMenuOpen)
})
</script>

<template>
  <ModerationPlaceholder
    v-if="isPurgedStub"
    kind="thread"
    reason="purged"
    :primary-id="thread.thread_id.substring(0, 12)"
    :secondary-text="thread.thread_last_activity ? `bumped ${timeAgo(thread.thread_last_activity)} ago` : ''"
    :can-show="false"
    :can-hide="!isHiddenBucket"
    hide-label="Hide thread"
    :can-restore="isHiddenBucket"
    restore-label="Unhide thread"
    :can-purge="false"
    :can-redownload="true"
    redownload-label="Undo purge — re-fetch from network"
    @hide="emit('hide-thread', thread)"
    @restore="emit('restore-thread', thread)"
    @redownload="emit('unpurge-thread', thread)"
  />

  <ModerationPlaceholder
    v-else-if="thread._isStub && isAwaitingNetwork"
    kind="thread"
    reason="awaiting_network"
    :primary-id="thread.thread_id.substring(0, 12)"
    secondary-text="Purge undone — waiting for network sync…"
    :can-show="false"
    :can-restore="false"
    :can-purge="false"
    :can-redownload="false"
  />

  <div
    v-else
    class="thread-card"
    :class="{ 'is-hidden-card': isHiddenBucket }"
    @click="emit('open-thread', thread.thread_id)"
  >
    <div class="tc-menu-wrap" @click.stop>
      <button
        class="tc-menu-btn"
        :class="{ active: openMenu }"
        title="Thread options"
        @click.stop="toggleMenu"
      >⋮</button>
      <div v-if="openMenu" class="tc-menu">
        <button
          v-if="isPinned"
          class="dm-item"
          @click="emitAction('unpin-thread')"
        >Unpin thread</button>
        <button
          v-else
          class="dm-item"
          @click="emitAction('pin-thread')"
        >Pin thread</button>
        <div class="dm-sep"></div>
        <button
          v-if="!isHiddenBucket"
          class="dm-item"
          @click="emitAction('hide-thread')"
        >Hide thread</button>
        <button
          v-else
          class="dm-item"
          @click="emitAction('restore-thread')"
        >Unhide</button>
        <div class="dm-sep"></div>
        <button
          class="dm-item dm-danger"
          @click="emitAction('purge-thread')"
        >Purge locally</button>
        <template v-if="hasIdentity">
          <div class="dm-sep"></div>
          <button
            v-if="isHiddenBucket && threadReason === 'hidden_identity'"
            class="dm-item"
            @click="emitAction('unhide-identity')"
          >Unhide identity</button>
          <button
            v-if="!isHiddenBucket"
            class="dm-item"
            @click="emitAction('hide-identity')"
          >Hide identity</button>
          <button
            class="dm-item dm-danger"
            @click="emitAction('ban-identity')"
          >Ban identity</button>
        </template>
      </div>
    </div>

    <div v-if="isPinned" class="tc-pin-badge" title="Pinned">📌</div>

    <div
      v-if="thread.op_attachment_content_hash && moderation.isAttachmentBanned(thread.op_attachment_content_hash)"
      class="tc-att-banned"
    >
      <span>Attachment banned</span>
    </div>

    <div
      v-else-if="attachmentState.hasOnlyBannedLocalFiles"
      class="tc-att-banned"
    >
      <span>Banned local file</span>
    </div>

    <div v-else-if="attachmentState.attachments.length" class="tc-loaded-bundle">
      <AttachmentBundle
        v-if="attachmentState.visibleAttachmentInfo?.isPreviewable"
        compact
        :attachments="[attachmentState.visibleAttachment]"
        :downloadable="false"
        :summary-text="attachmentState.summary.summaryText"
        :count-label="attachmentState.summary.countLabel"
      />
      <div
        v-else
        class="tc-file"
        :data-kind="attachmentState.visibleAttachmentInfo?.category"
      >
        <svg class="tc-file-icon" viewBox="0 0 24 24" aria-hidden="true">
          <path
            d="M7 3.5h7l4.5 4.5v12.5H7z"
            fill="none"
            stroke="currentColor"
            stroke-width="1.4"
            stroke-linejoin="round"
          />
          <path
            d="M14 3.5v4.5h4.5"
            fill="none"
            stroke="currentColor"
            stroke-width="1.4"
            stroke-linejoin="round"
          />
        </svg>
        <span class="tc-file-type">
          {{ attachmentState.summary.isMulti ? attachmentState.summary.countLabel : attachmentState.visibleAttachmentInfo?.typeLabel }}
        </span>
        <span v-if="attachmentState.summary.isMulti" class="tc-file-sub">
          visible file: {{ attachmentState.visibleAttachmentInfo?.typeLabel }}
        </span>
        <span class="tc-file-meta">{{ attachmentState.summary.summaryText }}</span>
      </div>
    </div>

    <div v-else-if="attachmentState.issue" class="tc-att-warning">
      <span class="tc-att-warning-title">{{ attachmentState.summary.countLabel }}</span>
      <span class="tc-att-warning-meta">{{ attachmentState.issue.message }}</span>
    </div>

    <div v-else-if="attachmentState.showProgress" class="tc-deferred tc-progress">
      <AttachmentFetchProgress :progress="attachmentState.progress" compact />
      <div class="tc-progress-actions">
        <button class="btn-dim" @click.stop="emit('pause-attachments', thread)">Pause</button>
        <button class="btn-dim danger" @click.stop="emit('cancel-attachments', thread)">Cancel</button>
      </div>
    </div>

    <div v-else-if="attachmentState.showPaused" class="tc-deferred tc-progress">
      <AttachmentFetchProgress :progress="attachmentState.progress" compact />
      <div class="tc-progress-actions">
        <button class="btn-load" @click.stop="emit('resume-attachments', thread)">Resume</button>
        <button class="btn-dim danger" @click.stop="emit('cancel-attachments', thread)">Cancel</button>
      </div>
    </div>

    <div v-else-if="attachmentState.showRetry" class="tc-deferred tc-progress">
      <AttachmentFetchProgress :progress="attachmentState.progress" compact />
      <div class="tc-progress-actions">
        <button class="btn-load" @click.stop="emit('retry-attachments', thread)">Retry</button>
        <button class="btn-dim danger" @click.stop="emit('cancel-attachments', thread)">Cancel</button>
      </div>
    </div>

    <div
      v-else-if="attachmentState.showLoadButton"
      class="tc-deferred"
      @click.stop="emit('load-attachments', thread)"
    >
      <span class="defer-icon">⊘</span>
      <span>Load {{ attachmentState.summary.summaryText }}</span>
    </div>

    <div class="tc-body">
      <div v-if="isHiddenBucket" class="tc-hidden-row">
        <span class="tc-badge hidden">{{ threadHiddenReason || 'hidden' }}</span>
        <button class="btn-dim" @click.stop="emit('restore-thread', thread)">Unhide</button>
      </div>
      <div class="tc-meta">
        <span class="tc-id">{{ thread.thread_id.substring(0, 8) }}</span>
        <span v-if="thread.has_attachments" class="tc-badge file">{{ attachmentState.badgeLabel }}</span>
        <span v-if="thread.text_only" class="tc-badge txt">TXT</span>
        <span class="tc-stats">{{ thread.post_count }} {{ thread.post_count === 1 ? 'post' : 'posts' }}</span>
      </div>
      <div v-if="thread.has_attachments" class="tc-attachment-summary">
        {{ attachmentState.summary.summaryText }}
      </div>
      <div class="tc-preview">{{ truncText(previewText) || '…' }}</div>
      <div class="tc-footer">bumped {{ timeAgo(thread.thread_last_activity) }} ago</div>
    </div>
  </div>
</template>

<style scoped>
.thread-card {
  background: #161630;
  border: 1px solid #2a2a4a;
  border-radius: 3px;
  cursor: pointer;
  transition: border-color 0.15s;
  display: flex;
  flex-direction: column;
  overflow: visible;
  position: relative;
}

.thread-card.is-hidden-card {
  border-color: #4a4440;
  background: #191720;
}

.thread-card:hover {
  border-color: #5050a0;
}

.tc-menu-wrap {
  position: absolute;
  top: 4px;
  right: 4px;
  z-index: 10;
}

.tc-menu-btn {
  background: rgba(14, 14, 32, 0.75);
  border: 1px solid transparent;
  color: #606080;
  font-size: 1rem;
  line-height: 1;
  padding: 0.1rem 0.3rem;
  border-radius: 3px;
  cursor: pointer;
  transition: color 0.15s, border-color 0.15s, background 0.15s;
}

.tc-menu-btn:hover,
.tc-menu-btn.active {
  color: #9090c0;
  border-color: #3a3a5a;
  background: rgba(26, 26, 48, 0.95);
}

.tc-menu {
  position: absolute;
  top: calc(100% + 3px);
  right: 0;
  background: #1a1a32;
  border: 1px solid #3a3a60;
  border-radius: 4px;
  min-width: 190px;
  padding: 0.25rem 0;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.55);
}

.dm-item {
  display: block;
  width: 100%;
  text-align: left;
  background: none;
  border: none;
  padding: 0.4rem 0.8rem;
  font-family: inherit;
  font-size: 0.77rem;
  color: #a0a0c0;
  cursor: pointer;
}

.dm-item:hover {
  background: #252545;
  color: #d0d0ff;
}

.dm-item.dm-danger {
  color: #b07070;
}

.dm-item.dm-danger:hover {
  background: #2a1818;
  color: #d09090;
}

.dm-sep {
  height: 1px;
  background: #2a2a48;
  margin: 0.25rem 0;
}

.tc-loaded-bundle {
  width: 100%;
  background: #101125;
}

.tc-file {
  width: 100%;
  aspect-ratio: 4 / 3;
  background: linear-gradient(180deg, #13132a 0%, #0f1023 100%);
  color: #93a0c8;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0.35rem;
  border-bottom: 1px solid #222246;
}

.tc-file[data-kind="archive"] {
  color: #d0b27a;
}

.tc-file[data-kind="document"] {
  color: #7cb6d8;
}

.tc-file[data-kind="audio"] {
  color: #a8c98a;
}

.tc-file[data-kind="text"] {
  color: #b6a4de;
}

.tc-file-icon {
  width: 2.25rem;
  height: 2.25rem;
  opacity: 0.85;
}

.tc-file-type {
  font-size: 0.78rem;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-align: center;
}

.tc-file-sub {
  font-size: 0.62rem;
  color: #7d86ab;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.tc-file-meta {
  font-size: 0.62rem;
  color: #626b90;
  text-align: center;
  max-width: 88%;
}

.tc-pin-badge {
  position: absolute;
  top: 5px;
  left: 5px;
  z-index: 5;
  font-size: 0.75rem;
  line-height: 1;
  pointer-events: none;
  filter: drop-shadow(0 0 2px #000);
}

.tc-att-banned {
  width: 100%;
  aspect-ratio: 4 / 3;
  background: #160f14;
  border: 1px dashed #5a3040;
  border-radius: 3px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 0.65rem;
  color: #c8a0a0;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.tc-att-warning {
  width: 100%;
  aspect-ratio: 4 / 3;
  background: linear-gradient(180deg, #171322 0%, #130f1c 100%);
  color: #c6b2c8;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0.45rem;
  border-bottom: 1px solid #222246;
}

.tc-att-warning-title {
  font-size: 0.78rem;
  font-weight: 700;
  letter-spacing: 0.05em;
  color: #d8c0da;
}

.tc-att-warning-meta {
  max-width: 90%;
  font-size: 0.64rem;
  line-height: 1.35;
  color: #8f7f98;
  text-align: center;
}

.tc-deferred {
  width: 100%;
  aspect-ratio: 4 / 3;
  background: #12122a;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0.3rem;
  color: #505070;
  font-size: 0.72rem;
  cursor: pointer;
}

.tc-deferred:hover {
  background: #1a1a3a;
  color: #7070a0;
}

.defer-icon {
  font-size: 1.4rem;
  opacity: 0.5;
}

.tc-body {
  padding: 0.5rem 0.6rem;
  flex: 1;
  display: flex;
  flex-direction: column;
}

.tc-hidden-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  margin-bottom: 0.4rem;
}

.tc-meta {
  display: flex;
  align-items: center;
  gap: 0.3rem;
  margin-bottom: 0.25rem;
}

.tc-id {
  font-size: 0.6rem;
  color: #505070;
  font-family: monospace;
}

.tc-stats {
  font-size: 0.65rem;
  color: #606070;
  margin-left: auto;
}

.tc-badge {
  font-size: 0.5rem;
  padding: 0.05rem 0.2rem;
  border-radius: 2px;
}

.tc-badge.file {
  background: #2a1a2a;
  color: #ff80c0;
}

.tc-badge.txt {
  background: #2a2a5a;
  color: #a0a0ff;
}

.tc-badge.hidden {
  background: #2b241e;
  color: #d0b090;
}

.tc-preview {
  flex: 1;
  font-size: 0.75rem;
  color: #9090a0;
  line-height: 1.35;
  overflow: hidden;
  word-break: break-word;
  display: -webkit-box;
  -webkit-line-clamp: 4;
  -webkit-box-orient: vertical;
}

.tc-attachment-summary {
  margin-bottom: 0.3rem;
  font-size: 0.64rem;
  color: #687098;
}

.tc-footer {
  font-size: 0.6rem;
  color: #404050;
  margin-top: 0.3rem;
}

.tc-progress {
  padding: 0.55rem 0.7rem;
}

.tc-progress-actions {
  display: flex;
  gap: 0.35rem;
  margin-top: 0.45rem;
  justify-content: center;
  flex-wrap: wrap;
}

.btn-dim,
.btn-load {
  font-family: inherit;
  border-radius: 3px;
  cursor: pointer;
}

.btn-dim {
  font-size: 0.78rem;
  padding: 0.3rem 0.7rem;
  background: transparent;
  color: #606070;
  border: 1px solid #303040;
}

.btn-dim.danger {
  color: #d09090;
  border-color: #5a3030;
}

.btn-load {
  font-size: 0.75rem;
  padding: 0.35rem 0.8rem;
  border: 1px dashed #4040a0;
  background: #1a1a30;
  color: #8080c0;
}

.btn-load:hover {
  background: #2a2a4a;
  color: #c0c0ff;
  border-style: solid;
}

:deep(.mod-placeholder.thread) {
  min-height: 140px;
  display: flex;
  flex-direction: column;
  justify-content: center;
}
</style>
