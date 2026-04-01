<script setup>
import AttachmentBundle from './AttachmentBundle.vue'
import AttachmentFetchProgress from './AttachmentFetchProgress.vue'

const props = defineProps({
  post: {
    type: Object,
    required: true,
  },
  state: {
    type: Object,
    required: true,
  },
  attachmentBanned: {
    type: Boolean,
    default: false,
  },
})

const emit = defineEmits([
  'open',
  'ban-file',
  'unban-file',
  'hide-placeholder',
  'pause',
  'resume',
  'retry',
  'cancel',
  'load',
])
</script>

<template>
  <div v-if="state.hasVisibleAttachments" class="post-attachments">
    <AttachmentBundle
      :attachments="state.renderableAttachments"
      :collapse-after="1"
      :summary-text="state.summary.summaryText"
      :count-label="state.summary.countLabel"
      interactive-preview
      show-file-ban-actions
      show-placeholder-actions
      @open="emit('open', post, $event)"
      @ban-file="emit('ban-file', post, $event)"
      @unban-file="emit('unban-file', post, $event)"
      @hide-placeholder="emit('hide-placeholder', $event)"
    />
  </div>

  <div
    v-else-if="attachmentBanned"
    class="att-banned-placeholder"
  >
    <span class="att-banned-label">Attachment banned — content removed</span>
  </div>

  <div
    v-else-if="state.hasOnlyHiddenBannedFiles"
    class="att-banned-placeholder"
  >
    <span class="att-banned-label">Banned local files hidden</span>
  </div>

  <div
    v-else-if="state.issue"
    class="att-warning-placeholder"
  >
    <span class="att-warning-title">{{ state.summary.countLabel }}</span>
    <span class="att-warning-meta">{{ state.issue.message }}</span>
  </div>

  <div v-else-if="state.showProgress" class="post-attachments-deferred loading">
    <AttachmentFetchProgress :progress="state.progress" />
    <div class="attachment-progress-actions">
      <button class="btn-dim" @click.stop="emit('pause', post)">Pause</button>
      <button class="btn-dim danger" @click.stop="emit('cancel', post)">Cancel</button>
    </div>
  </div>

  <div
    v-else-if="state.showPaused"
    class="post-attachments-deferred loading"
  >
    <AttachmentFetchProgress :progress="state.progress" />
    <div class="attachment-progress-actions">
      <button class="btn-load" @click.stop="emit('resume', post)">Resume</button>
      <button class="btn-dim danger" @click.stop="emit('cancel', post)">Cancel</button>
    </div>
  </div>

  <div
    v-else-if="state.showRetry"
    class="post-attachments-deferred loading"
  >
    <AttachmentFetchProgress :progress="state.progress" />
    <div class="attachment-progress-actions">
      <button class="btn-load" @click.stop="emit('retry', post)">Retry</button>
      <button class="btn-dim danger" @click.stop="emit('cancel', post)">Cancel</button>
    </div>
  </div>

  <div
    v-else-if="state.showLoadButton"
    class="post-attachments-deferred"
  >
    <button class="btn-load" @click.stop="emit('load', post)">
      Load {{ state.summary.summaryText }}
    </button>
  </div>
</template>

<style scoped>
.post-attachments {
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
  max-width: 250px;
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

.post-attachments-deferred {
  flex-shrink: 0;
}

.post-attachments-deferred.loading {
  min-width: 220px;
}

.btn-load {
  font-family: inherit;
  font-size: 0.75rem;
  padding: 0.35rem 0.8rem;
  border-radius: 3px;
  cursor: pointer;
  border: 1px dashed #4040a0;
  background: #1a1a30;
  color: #8080c0;
}

.btn-load:hover {
  background: #2a2a4a;
  color: #c0c0ff;
  border-style: solid;
}

.btn-dim {
  font-family: inherit;
  font-size: 0.76rem;
  padding: 0.3rem 0.6rem;
  border-radius: 3px;
  cursor: pointer;
  border: 1px solid #303040;
  background: transparent;
  color: #9090b0;
}

.btn-dim:hover {
  background: #222238;
}

.btn-dim.danger {
  color: #d09090;
  border-color: #5a3030;
}

.attachment-progress-actions {
  display: flex;
  gap: 0.5rem;
  margin-top: 0.5rem;
}
</style>
