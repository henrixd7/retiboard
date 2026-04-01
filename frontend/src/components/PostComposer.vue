<script setup>
/**
 * Post composer — split-blob encryption.
 *
 * Encrypts text and attachments into separate blobs, sends both to the API
 * as multipart form-data: `payload` (text) + `attachment_payload` (attachments, optional).
 *
 * Supports external >>ref insertion via quotePost() (exposed to parent).
 */
import { ref, computed } from 'vue'
import { useBoardStore } from '../stores/boardStore.js'
import { useNotificationStore } from '../stores/notificationStore.js'
import { encryptPayload } from '../crypto/encrypt.js'
import { runTask } from '../crypto/workerClient.js'
import { encryptPing, generateEphemeralKey } from '../crypto/pings.js'
import {
  collectFilesFromDataTransfer,
  getDraftAttachmentName,
  hasFileDataTransfer,
} from '../utils/attachmentIntake.js'
import { apiRequest } from '../utils/api.js'

const props = defineProps({
  boardId: String,
  threadId: { type: String, default: null },
  isOp: { type: Boolean, default: true },
  availablePosts: { type: Array, default: () => [] },
})
const emit = defineEmits(['posted'])

const boardStore = useBoardStore()
const notifStore = useNotificationStore()
const text = ref('')
const files = ref([])
const posting = ref(false)
const postError = ref(null)
const powProgress = ref('')
const textareaRef = ref(null)
const fileInputRef = ref(null)
const dragActive = ref(false)
const dragDepth = ref(0)

const board = computed(() =>
  boardStore.boards.find(b => b.board_id === props.boardId)
)

// Default max length; boards could override via announce in the future.
const DEFAULT_MAX_LENGTH = 10_000

const maxLength = computed(() => {
  // Future: board.max_text_length from announce. For now, use default.
  return DEFAULT_MAX_LENGTH
})

const charCount = computed(() => text.value.length)
const canAttachFiles = computed(() => !!board.value && !board.value.text_only)

function formatNumber(n) {
  return n.toLocaleString('en-US').replace(/,/g, ' ')
}

function formatFileSize(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

/**
 * Insert a >>postId reference at the top of the textarea.
 * Called externally when user clicks reply arrow on a post.
 * Each reference goes on its own line. Leaves cursor after refs.
 */
function quotePost(postId) {
  const refStr = `>>${postId.substring(0, 10)}`
  const current = text.value

  // Check if this reference already exists anywhere in the text.
  if (current.includes(refStr)) return

  // Find the end of any existing >>ref block at the top.
  const lines = current.split('\n')
  let insertIdx = 0
  for (let i = 0; i < lines.length; i++) {
    if (/^>>[0-9a-fA-F]{6,64}$/.test(lines[i].trim())) {
      insertIdx = i + 1
    } else {
      break
    }
  }

  // Insert the new reference.
  lines.splice(insertIdx, 0, refStr)

  // Ensure there's a blank line between refs and body text.
  if (insertIdx + 1 < lines.length && lines[insertIdx + 1]?.trim() !== '' && !/^>>[0-9a-fA-F]/.test(lines[insertIdx + 1]?.trim())) {
    lines.splice(insertIdx + 1, 0, '')
  }

  text.value = lines.join('\n')

  // Focus textarea and place cursor after the refs block.
  if (textareaRef.value) {
    textareaRef.value.focus()
    const refBlock = lines.slice(0, insertIdx + 1).join('\n')
    const pos = refBlock.length + 1  // +1 for the newline after refs
    requestAnimationFrame(() => {
      textareaRef.value.setSelectionRange(pos, pos)
    })
  }
}

// Expose quotePost to parent via template ref.
defineExpose({ quotePost })

function handleFiles(e) {
  appendFiles(e.target.files)
  if (e.target) e.target.value = ''
}

function appendFiles(fileList) {
  if (!canAttachFiles.value) return

  const incoming = Array.from(fileList || []).filter((file) => file && typeof file.arrayBuffer === 'function')
  if (incoming.length === 0) return

  files.value = [...files.value, ...incoming]
  postError.value = null
}

function removeFile(index) {
  files.value = files.value.filter((_, fileIndex) => fileIndex !== index)
}

function clearFiles() {
  files.value = []
  if (fileInputRef.value) fileInputRef.value.value = ''
}

function resetDragState() {
  dragDepth.value = 0
  dragActive.value = false
}

function onDragEnter(e) {
  if (!hasFileDataTransfer(e.dataTransfer)) return
  e.preventDefault()
  if (!canAttachFiles.value) return
  dragDepth.value += 1
  dragActive.value = true
}

function onDragOver(e) {
  if (!hasFileDataTransfer(e.dataTransfer)) return
  e.preventDefault()
  if (!canAttachFiles.value) return
  dragActive.value = true
}

function onDragLeave(e) {
  if (!dragActive.value) return
  e.preventDefault()
  dragDepth.value = Math.max(0, dragDepth.value - 1)
  if (dragDepth.value === 0) dragActive.value = false
}

function onDrop(e) {
  if (hasFileDataTransfer(e.dataTransfer)) e.preventDefault()
  resetDragState()
  if (!canAttachFiles.value) return

  const incoming = collectFilesFromDataTransfer(e.dataTransfer)
  if (incoming.length === 0) return

  e.preventDefault()
  appendFiles(incoming)
}

function onPaste(e) {
  if (!canAttachFiles.value) return

  const incoming = collectFilesFromDataTransfer(e.clipboardData)
  if (incoming.length === 0) return

  e.preventDefault()
  appendFiles(incoming)
}

async function submitPost() {
  if (!text.value.trim() && files.value.length === 0) return
  posting.value = true
  postError.value = null
  powProgress.value = ''

  try {
    const key = await boardStore.getBoardKey(props.boardId)

    // Prepare attachments.
    const attachments = []
    if (board.value && !board.value.text_only) {
      for (const file of files.value) {
        const bytes = new Uint8Array(await file.arrayBuffer())
        attachments.push({
          filename: getDraftAttachmentName(file),
          mime_type: file.type || 'application/octet-stream',
          bytes,
        })
      }
    }

    // Encrypt: split-blob (text + attachments separately).
    const {
      textBlob, contentHash, payloadSize,
      attachmentBlob, attachmentContentHash, attachmentPayloadSize,
      hasAttachments,
    } = await encryptPayload(key, text.value, attachments)

    // Generate post ID and metadata.
    const postId = generateId()
    const threadId = props.isOp ? postId : props.threadId
    const timestamp = Math.floor(Date.now() / 1000)
    const { publicKeyHex, privateKeyObj } = await generateEphemeralKey()
    const encryptedPings = []
    const seenQuotedPosts = new Set()
    const mentionRegex = />>([0-9a-fA-F]{6,64})/g

    let match
    while ((match = mentionRegex.exec(text.value)) !== null) {
      const quotedHex = match[1].toLowerCase()
      const targetPost = props.availablePosts.find((candidate) => {
        const candidateId = String(candidate?.post_id || '').toLowerCase()
        return candidateId && candidateId.startsWith(quotedHex)
      })

      if (!targetPost?.post_id || !targetPost.public_key) continue
      if (seenQuotedPosts.has(targetPost.post_id)) continue

      encryptedPings.push(await encryptPing(targetPost.public_key, postId))
      seenQuotedPosts.add(targetPost.post_id)
    }

    const metadata = {
      post_id: postId,
      thread_id: threadId,
      parent_id: props.isOp ? '' : props.threadId,
      timestamp,
      bump_flag: true,
      content_hash: contentHash,
      payload_size: payloadSize,
      attachment_content_hash: attachmentContentHash,
      attachment_payload_size: attachmentPayloadSize,
      has_attachments: hasAttachments,
      attachment_count: attachments.length,
      text_only: board.value?.text_only || false,
      identity_hash: '',
      pow_nonce: '',
      public_key: publicKeyHex,
      encrypted_pings: encryptedPings,
      edit_signature: '',
    }

    // Solve PoW.
    const difficulty = board.value?.pow_difficulty || 0
    if (difficulty > 0) {
      powProgress.value = 'Solving PoW…'
      metadata.pow_nonce = await solvePoW(metadata, difficulty)
      powProgress.value = 'PoW solved!'
    }

    // Submit to backend.
    const formData = new FormData()
    formData.append('metadata', JSON.stringify(metadata))
    formData.append('payload', new Blob([textBlob]), 'payload.bin')

    // Attach the encrypted attachment blob if present.
    if (attachmentBlob) {
      formData.append('attachment_payload', new Blob([attachmentBlob]), 'attachment_payload.bin')
    }

    await apiRequest(`/api/boards/${props.boardId}/posts`, {
      method: 'POST',
      body: formData,
      parseAs: 'json',
      timeoutMs: 60_000,
    })

    // Register our post so we can detect replies to it.
    notifStore.registerOwnPost(postId, privateKeyObj)

    text.value = ''
    clearFiles()
    emit('posted')
  } catch (e) {
    postError.value = e.message
  } finally {
    posting.value = false
    powProgress.value = ''
  }
}

function generateId() {
  const bytes = new Uint8Array(16)
  crypto.getRandomValues(bytes)
  return Array.from(bytes).map(b => b.toString(16).padStart(2, '0')).join('')
}

async function solvePoW(meta, difficulty) {
  return runTask('solvePoW', { metadata: meta, difficulty }, [], (attempts) => {
    powProgress.value = `PoW: ${attempts} attempts…`
  })
}
</script>

<template>
  <div
    :class="['composer', { 'drag-active': dragActive }]"
    @dragenter="onDragEnter"
    @dragover="onDragOver"
    @dragleave="onDragLeave"
    @drop="onDrop"
  >
    <textarea
      ref="textareaRef"
      v-model="text"
      :placeholder="isOp ? 'Start a new thread…' : 'Reply…'"
      :maxlength="maxLength"
      class="text-input"
      rows="4"
      @paste="onPaste"
    ></textarea>
    <div class="composer-meta-row">
      <span :class="['char-counter', { warn: charCount > maxLength * 0.9 }]">
        {{ formatNumber(charCount) }} / {{ formatNumber(maxLength) }}
      </span>
    </div>

    <div v-if="canAttachFiles" class="file-row">
      <label class="file-label">
        <input ref="fileInputRef" type="file" multiple @change="handleFiles" class="file-input" />
        {{ files.length > 0 ? `${files.length} file(s) selected` : 'Attach, drop, or paste files' }}
      </label>
    </div>

    <div v-if="canAttachFiles && files.length > 0" class="file-list">
      <div
        v-for="(file, index) in files"
        :key="`${getDraftAttachmentName(file)}:${file.size}:${index}`"
        class="file-pill"
      >
        <span class="file-pill-name">{{ getDraftAttachmentName(file) }}</span>
        <span class="file-pill-meta">{{ formatFileSize(file.size) }}</span>
        <button class="file-pill-remove" @click="removeFile(index)" :disabled="posting" title="Remove attachment">✕</button>
      </div>
      <button class="clear-files-btn" @click="clearFiles" :disabled="posting">Clear all</button>
    </div>

    <div v-if="dragActive" class="drop-hint">
      Drop files to attach them to this {{ isOp ? 'thread' : 'reply' }}.
    </div>

    <div v-if="powProgress" class="pow-status">{{ powProgress }}</div>
    <div v-if="postError" class="error">{{ postError }}</div>

    <button
      class="btn submit-btn"
      @click="submitPost"
      :disabled="posting || (!text.trim() && files.length === 0)"
    >
      {{ posting ? 'Posting…' : (isOp ? 'Create Thread' : 'Post Reply') }}
    </button>
  </div>
</template>

<style scoped>
.composer {
  background: #12122a; border: 1px solid #2a2a4a; border-radius: 3px;
  padding: 0.8rem; margin: 0.8rem 0; display: flex; flex-direction: column; gap: 0.5rem;
}
.composer.drag-active {
  border-color: #7070d0;
  box-shadow: 0 0 0 1px rgba(112, 112, 208, 0.35);
}
.text-input {
  font-family: inherit; font-size: 0.85rem; padding: 0.5rem;
  background: #0e0e24; border: 1px solid #3a3a5a; border-radius: 3px;
  color: #d0d0e0; resize: vertical; outline: none; min-height: 80px;
}
.text-input:focus { border-color: #6060c0; }
.composer-meta-row {
  display: flex; justify-content: flex-end; align-items: center;
}
.char-counter {
  font-size: 0.68rem; color: #404058; font-variant-numeric: tabular-nums;
}
.char-counter.warn { color: #c08040; }
.file-row { display: flex; align-items: center; }
.file-label {
  font-size: 0.78rem; color: #808090; cursor: pointer;
  background: #1a1a3a; padding: 0.3rem 0.6rem; border-radius: 3px;
  border: 1px dashed #3a3a5a;
}
.file-label:hover { border-color: #5050a0; }
.file-input { display: none; }
.file-list {
  display: flex; flex-wrap: wrap; gap: 0.4rem;
  align-items: center;
}
.file-pill {
  display: inline-flex; align-items: center; gap: 0.45rem;
  max-width: 100%;
  background: #181836; color: #c0c0d0;
  border: 1px solid #31315a; border-radius: 999px;
  padding: 0.2rem 0.35rem 0.2rem 0.55rem;
}
.file-pill-name {
  font-size: 0.74rem; max-width: 220px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.file-pill-meta {
  font-size: 0.68rem; color: #7f7fa0;
}
.file-pill-remove,
.clear-files-btn {
  font-family: inherit; font-size: 0.72rem; cursor: pointer;
  border-radius: 999px; border: 1px solid #3a3a5a;
  background: #222248; color: #c0c0ff;
}
.file-pill-remove {
  width: 1.35rem; height: 1.35rem; line-height: 1;
  padding: 0;
}
.clear-files-btn {
  padding: 0.22rem 0.65rem;
}
.file-pill-remove:hover,
.clear-files-btn:hover {
  background: #2c2c58;
}
.drop-hint {
  font-size: 0.74rem; color: #9aa0d8;
  background: rgba(46, 46, 96, 0.45);
  border: 1px dashed #5a5ab0; border-radius: 4px;
  padding: 0.45rem 0.55rem;
}
.identity-row { display: flex; align-items: center; }
.identity-label {
  font-size: 0.72rem; color: #7070a0; cursor: pointer;
  display: flex; align-items: center; gap: 0.35rem; user-select: none;
}
.identity-label:hover { color: #9090c0; }
.identity-check { cursor: pointer; accent-color: #5050a0; }
.pow-status { font-size: 0.75rem; color: #c0c060; }
.error { font-size: 0.75rem; color: #ff8080; }
.btn {
  font-family: inherit; font-size: 0.8rem; padding: 0.4rem 1rem;
  border-radius: 3px; cursor: pointer; border: 1px solid #4040a0;
  background: #2a2a5a; color: #c0c0ff; align-self: flex-start;
}
.btn:hover { background: #3a3a6a; }
.btn:disabled { opacity: 0.4; cursor: not-allowed; }
</style>
