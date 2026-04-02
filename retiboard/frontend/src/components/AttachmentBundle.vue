<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  getAttachmentDownloadName,
  getAttachmentTypeLabel,
  normalizeAttachmentFilename,
  isAudioType,
  isImageType,
  isVideoType,
} from '../utils/attachments.js'
import { INLINE_ATTACHMENT_RENDER_LIMIT } from '../utils/attachmentSummary.js'
import { isLocalBannedFilePlaceholder } from '../utils/attachmentModeration.js'

const props = defineProps({
  attachments: {
    type: Array,
    default: () => [],
  },
  compact: {
    type: Boolean,
    default: false,
  },
  interactivePreview: {
    type: Boolean,
    default: false,
  },
  downloadable: {
    type: Boolean,
    default: true,
  },
  showFileBanActions: {
    type: Boolean,
    default: false,
  },
  showPlaceholderActions: {
    type: Boolean,
    default: false,
  },
  collapseAfter: {
    type: Number,
    default: INLINE_ATTACHMENT_RENDER_LIMIT,
  },
  summaryText: {
    type: String,
    default: '',
  },
  countLabel: {
    type: String,
    default: '',
  },
})

const emit = defineEmits(['open', 'ban-file', 'unban-file', 'hide-placeholder'])

const expanded = ref(false)
const openMenuKey = ref(null)
const blobUrlCache = new WeakMap()
const activeBlobUrls = new Set()

const attachmentSignature = computed(() => {
  const items = Array.isArray(props.attachments) ? props.attachments : []
  return items.map((attachment, index) => (
    `${attachment?.file_hash || attachment?.filename || attachment?.mime_type || 'att'}:${attachment?.placeholder_kind || 'live'}:${index}`
  )).join('|')
})

const visibleAttachments = computed(() => {
  const items = Array.isArray(props.attachments) ? props.attachments : []
  if (props.compact) return items.slice(0, 1)
  if (expanded.value || items.length <= props.collapseAfter) return items
  return items.slice(0, props.collapseAfter)
})

const hiddenCount = computed(() => {
  const total = Array.isArray(props.attachments) ? props.attachments.length : 0
  if (props.compact) return Math.max(0, total - 1)
  return Math.max(0, total - props.collapseAfter)
})

function cleanupObjectUrls() {
  for (const url of activeBlobUrls) {
    try { URL.revokeObjectURL(url) } catch {}
  }
  activeBlobUrls.clear()
}

watch(
  attachmentSignature,
  (next, prev) => {
    if (prev && next === prev) return
    expanded.value = false
    openMenuKey.value = null
    cleanupObjectUrls()
  }
)

function closeMenu() {
  openMenuKey.value = null
}

function onDocClick(event) {
  if (!event.target.closest('.att-menu-wrap')) closeMenu()
}

onMounted(() => {
  document.addEventListener('click', onDocClick)
})

onBeforeUnmount(() => {
  document.removeEventListener('click', onDocClick)
  cleanupObjectUrls()
})

function attachmentSize(att) {
  return Number(att?.blob?.size || att?.bytes?.length || att?.bytes?.byteLength || 0)
}

function blobUrl(att) {
  if (blobUrlCache.has(att)) return blobUrlCache.get(att)
  const source = att?.blob || (att?.bytes ? new Blob([att.bytes], { type: att.mime_type }) : null)
  if (!source) return ''
  const url = URL.createObjectURL(source)
  activeBlobUrls.add(url)
  blobUrlCache.set(att, url)
  return url
}

function isImage(att) { return isImageType(att?.mime_type) }
function isVideo(att) { return isVideoType(att?.mime_type) }
function isAudio(att) { return isAudioType(att?.mime_type) }
function attachmentTypeLabel(att) { return getAttachmentTypeLabel(att?.mime_type) }
function attachmentFilename(att) { return normalizeAttachmentFilename(att?.filename) }
function attachmentDownloadName(att) { return getAttachmentDownloadName(att) }

function prettySize(bytes) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function attachmentMeta(att) {
  return `${att?.mime_type || 'application/octet-stream'} · ${prettySize(attachmentSize(att))}`
}

function openAttachment(att) {
  if (!props.interactivePreview) return
  emit('open', att)
}

function onPreviewClick(event, att) {
  if (!props.interactivePreview) return
  event.stopPropagation()
  openAttachment(att)
}

function isPlaceholder(att) {
  return isLocalBannedFilePlaceholder(att)
}

function emitBanFile(att) {
  emit('ban-file', att)
}

function emitUnbanFile(att) {
  emit('unban-file', att)
}

function emitHidePlaceholder(att) {
  emit('hide-placeholder', att)
}

function itemKey(att, index) {
  return `${att?.file_hash || att?.filename || att?.mime_type || 'att'}:${att?.placeholder_kind || 'live'}:${index}`
}

function toggleMenuFor(att, index) {
  const key = itemKey(att, index)
  openMenuKey.value = openMenuKey.value === key ? null : key
}

function isMenuOpen(att, index) {
  return openMenuKey.value === itemKey(att, index)
}

function onBanFile(att) {
  closeMenu()
  emitBanFile(att)
}

function onUnbanFile(att) {
  closeMenu()
  emitUnbanFile(att)
}

function onHidePlaceholder(att) {
  closeMenu()
  emitHidePlaceholder(att)
}
</script>

<template>
  <div :class="['rb-attachment-bundle', { compact }]">
    <template v-for="(att, index) in visibleAttachments" :key="itemKey(att, index)">
      <div v-if="isPlaceholder(att)" class="attachment-item att-placeholder">
        <div v-if="showPlaceholderActions" class="att-menu-wrap">
          <button class="att-menu-btn" @click.stop="toggleMenuFor(att, index)">⋮</button>
          <div v-if="isMenuOpen(att, index)" class="att-menu">
            <button class="att-menu-item" @click.stop="onUnbanFile(att)">Unban</button>
            <button class="att-menu-item" @click.stop="onHidePlaceholder(att)">Hide</button>
          </div>
        </div>
        <div class="att-placeholder-label">Banned local file</div>
        <div class="att-placeholder-meta">
          <span v-if="attachmentFilename(att)" class="att-filename">{{ attachmentFilename(att) }}</span>
          <span class="att-meta">{{ attachmentTypeLabel(att) }} · {{ prettySize(attachmentSize(att)) }}</span>
        </div>
      </div>

      <div v-else-if="isImage(att)" class="attachment-item" :class="{ interactive: interactivePreview }" @click="onPreviewClick($event, att)">
        <div v-if="showFileBanActions" class="att-menu-wrap">
          <button class="att-menu-btn" @click.stop="toggleMenuFor(att, index)">⋮</button>
          <div v-if="isMenuOpen(att, index)" class="att-menu">
            <button class="att-menu-item att-menu-item-danger" @click.stop="onBanFile(att)">Ban file locally</button>
          </div>
        </div>
        <img :src="blobUrl(att)" class="thumb-img" />
        <div class="att-inline-meta">
          <span v-if="attachmentFilename(att)" class="att-filename">{{ attachmentFilename(att) }}</span>
          <span class="att-meta">{{ attachmentMeta(att) }}</span>
        </div>
      </div>

      <div v-else-if="isVideo(att)" class="attachment-item" :class="{ interactive: interactivePreview }" @click="onPreviewClick($event, att)">
        <div v-if="showFileBanActions" class="att-menu-wrap">
          <button class="att-menu-btn" @click.stop="toggleMenuFor(att, index)">⋮</button>
          <div v-if="isMenuOpen(att, index)" class="att-menu">
            <button class="att-menu-item att-menu-item-danger" @click.stop="onBanFile(att)">Ban file locally</button>
          </div>
        </div>
        <video :src="blobUrl(att)" class="thumb-vid" muted preload="metadata" playsinline />
        <div class="play-badge">▶</div>
        <div class="att-inline-meta">
          <span v-if="attachmentFilename(att)" class="att-filename">{{ attachmentFilename(att) }}</span>
          <span class="att-meta">{{ attachmentMeta(att) }}</span>
        </div>
      </div>

      <div v-else-if="isAudio(att)" class="attachment-item audio-item">
        <div v-if="showFileBanActions" class="att-menu-wrap">
          <button class="att-menu-btn" @click.stop="toggleMenuFor(att, index)">⋮</button>
          <div v-if="isMenuOpen(att, index)" class="att-menu">
            <button class="att-menu-item att-menu-item-danger" @click.stop="onBanFile(att)">Ban file locally</button>
          </div>
        </div>
        <audio :src="blobUrl(att)" controls class="inline-audio" @click.stop />
        <div class="att-inline-meta">
          <span v-if="attachmentFilename(att)" class="att-filename">{{ attachmentFilename(att) }}</span>
          <span class="att-meta">{{ attachmentMeta(att) }}</span>
        </div>
      </div>

      <div v-else class="attachment-item file-item">
        <div v-if="showFileBanActions" class="att-menu-wrap">
          <button class="att-menu-btn" @click.stop="toggleMenuFor(att, index)">⋮</button>
          <div v-if="isMenuOpen(att, index)" class="att-menu">
            <button class="att-menu-item att-menu-item-danger" @click.stop="onBanFile(att)">Ban file locally</button>
          </div>
        </div>
        <a
          v-if="downloadable"
          :href="blobUrl(att)"
          :download="attachmentDownloadName(att)"
          class="att-download"
          @click.stop
        >
          <span class="att-download-type">{{ attachmentTypeLabel(att) }}</span>
          <span v-if="attachmentFilename(att)" class="att-download-name">{{ attachmentFilename(att) }}</span>
          <span class="att-download-meta">{{ attachmentMeta(att) }}</span>
        </a>
        <div v-else class="att-download static-download">
          <span class="att-download-type">{{ attachmentTypeLabel(att) }}</span>
          <span v-if="attachmentFilename(att)" class="att-download-name">{{ attachmentFilename(att) }}</span>
          <span class="att-download-meta">{{ attachmentMeta(att) }}</span>
        </div>
      </div>
    </template>

    <div v-if="summaryText" class="bundle-summary">
      <span class="bundle-summary-text">{{ summaryText }}</span>
      <span v-if="compact && hiddenCount > 0" class="bundle-summary-count">+{{ hiddenCount }} more</span>
    </div>

    <button
      v-if="!compact && hiddenCount > 0 && !expanded"
      class="bundle-toggle"
      @click.stop="expanded = true"
    >
      Show {{ hiddenCount }} more file{{ hiddenCount === 1 ? '' : 's' }}
    </button>
    <button
      v-else-if="!compact && hiddenCount > 0 && expanded"
      class="bundle-toggle"
      @click.stop="expanded = false"
    >
      Collapse files
    </button>
  </div>
</template>

<style scoped>
.rb-attachment-bundle {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}

.rb-attachment-bundle.compact {
  gap: 0;
}

.attachment-item {
  position: relative;
}

.attachment-item.interactive {
  cursor: pointer;
}

.att-menu-wrap {
  position: absolute;
  top: 4px;
  right: 4px;
  z-index: 20;
}

.att-menu-btn {
  background: rgba(14, 14, 32, 0.82);
  border: 1px solid transparent;
  color: #6d6d92;
  font-size: 0.95rem;
  line-height: 1;
  padding: 0.08rem 0.28rem;
  border-radius: 3px;
  cursor: pointer;
}

.att-menu-btn:hover,
.att-menu-btn:focus-visible {
  color: #c0c0ff;
  border-color: #3a3a5a;
  background: rgba(26, 26, 48, 0.96);
  outline: none;
}

.att-menu {
  position: absolute;
  top: calc(100% + 3px);
  right: 0;
  min-width: 150px;
  background: #1a1a32;
  border: 1px solid #3a3a60;
  border-radius: 4px;
  padding: 0.25rem 0;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.55);
}

.att-menu-item {
  display: block;
  width: 100%;
  text-align: left;
  background: none;
  border: none;
  padding: 0.4rem 0.8rem;
  font-family: inherit;
  font-size: 0.74rem;
  color: #c2c2da;
  cursor: pointer;
}

.att-menu-item:hover {
  background: #252545;
  color: #e0e0ff;
}

.att-menu-item-danger {
  color: #d5a6a6;
}

.att-menu-item-danger:hover {
  background: #2a1818;
  color: #efb0b0;
}

.thumb-img {
  max-width: 250px;
  max-height: 200px;
  border-radius: 3px;
  border: 1px solid #2a2a4a;
  display: block;
  transition: border-color 0.15s;
}

.thumb-img:hover {
  border-color: #5050a0;
}

.thumb-vid {
  max-width: 250px;
  max-height: 200px;
  border-radius: 3px;
  border: 1px solid #2a2a4a;
  display: block;
}

.play-badge {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  background: rgba(0, 0, 0, 0.6);
  color: #fff;
  width: 36px;
  height: 36px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 0.9rem;
  pointer-events: none;
}

.inline-audio {
  max-width: 240px;
}

.att-inline-meta {
  display: flex;
  flex-direction: column;
  gap: 0.1rem;
  margin-top: 0.25rem;
  font-size: 0.68rem;
  line-height: 1.35;
}

.att-filename {
  color: #c0c8ff;
  word-break: break-word;
}

.att-meta {
  color: #8088b8;
  word-break: break-word;
}

.att-download {
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
  font-size: 0.72rem;
  color: #7070a0;
  background: #1a1a30;
  padding: 0.3rem 0.5rem;
  border-radius: 3px;
  text-decoration: none;
}

.att-download-type {
  color: #c0c8ff;
  font-weight: 700;
  letter-spacing: 0.06em;
}

.att-download-name {
  color: #c0c8ff;
  word-break: break-word;
}

.att-download-meta {
  color: #8088b8;
}

.att-download:hover {
  color: #a0a0ff;
  background: #20204a;
}

.static-download {
  cursor: default;
}

.att-placeholder {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  padding: 0.5rem 0.6rem;
  border: 1px dashed #5a3040;
  border-radius: 4px;
  background: #160f14;
}

.att-placeholder-label {
  font-size: 0.68rem;
  color: #d9b0b0;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.att-placeholder-meta {
  display: flex;
  flex-direction: column;
  gap: 0.1rem;
  font-size: 0.68rem;
}

.bundle-summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  font-size: 0.66rem;
  color: #70789e;
  margin-top: 0.1rem;
}

.bundle-summary-text {
  word-break: break-word;
}

.bundle-summary-count {
  color: #949bd0;
  white-space: nowrap;
}

.bundle-toggle {
  align-self: flex-start;
  background: none;
  border: none;
  padding: 0;
  color: #8088c8;
  cursor: pointer;
  font-family: inherit;
  font-size: 0.72rem;
  text-decoration: underline;
}

.bundle-toggle:hover {
  color: #c0c0ff;
}

.compact .thumb-img,
.compact .thumb-vid {
  width: 100%;
  max-width: none;
  max-height: none;
  aspect-ratio: 4 / 3;
  object-fit: contain;
  background: #0e0e20;
  border: none;
  border-bottom: 1px solid #222246;
  border-radius: 0;
}

.compact .file-item .att-download,
.compact .audio-item .att-download {
  min-height: 0;
}

@media (max-width: 500px) {
  .thumb-img,
  .thumb-vid {
    max-width: 100%;
  }
}
</style>
