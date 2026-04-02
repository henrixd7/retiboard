<script setup>
/**
 * Post preview overlay — shows referenced post content on hover.
 * Positioned near the mouse cursor, clamped to viewport bounds.
 */
import { ref, onMounted, onUnmounted, watch } from 'vue'

const props = defineProps({
  post: Object,       // The referenced post metadata
  text: String,       // Decrypted text of the referenced post
  anchorEl: Object,   // The DOM element (link) we're hovering over
})

const popupRef = ref(null)
const style = ref({})

function updatePosition() {
  if (!props.anchorEl || !popupRef.value) return

  const rect = props.anchorEl.getBoundingClientRect()
  const popup = popupRef.value.getBoundingClientRect()

  let top = rect.bottom + 4
  let left = rect.left

  // Clamp to viewport.
  if (top + popup.height > window.innerHeight - 8) {
    top = rect.top - popup.height - 4
  }
  if (left + popup.width > window.innerWidth - 8) {
    left = window.innerWidth - popup.width - 8
  }
  if (left < 8) left = 8
  if (top < 8) top = 8

  style.value = {
    position: 'fixed',
    top: `${top}px`,
    left: `${left}px`,
    zIndex: 2000,
  }
}

onMounted(() => {
  requestAnimationFrame(updatePosition)
})

watch(() => props.anchorEl, () => {
  requestAnimationFrame(updatePosition)
})
</script>

<template>
  <div ref="popupRef" class="post-preview" :style="style">
    <div v-if="post" class="pp-header">
      <span class="pp-id">{{ post.post_id?.substring(0, 10) }}</span>
      <span v-if="post.timestamp" class="pp-time">
        {{ new Date(post.timestamp * 1000).toLocaleString() }}
      </span>
      <span v-if="post.identity_hash" class="pp-identity">
        ID:{{ post.identity_hash.substring(0, 8) }}
      </span>
    </div>
    <div class="pp-body">
      <template v-if="text !== undefined && text !== null">
        {{ text.length > 500 ? text.substring(0, 500) + '…' : text }}
      </template>
      <span v-else class="pp-unavailable">Payload unavailable</span>
    </div>
  </div>
</template>

<style scoped>
.post-preview {
  max-width: 420px;
  min-width: 200px;
  background: #1a1a38;
  border: 1px solid #4040a0;
  border-radius: 4px;
  padding: 0.5rem 0.65rem;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.6);
  pointer-events: none;
  font-size: 0.8rem;
  line-height: 1.4;
  color: #c0c0d0;
}
.pp-header {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  font-size: 0.68rem;
  color: #606080;
  margin-bottom: 0.3rem;
  padding-bottom: 0.25rem;
  border-bottom: 1px solid #2a2a4a;
}
.pp-id { font-family: monospace; color: #8080c0; }
.pp-time { color: #505060; }
.pp-identity { color: #a0a060; }
.pp-body {
  white-space: pre-wrap;
  word-break: break-word;
  color: #b0b0c8;
}
.pp-unavailable {
  color: #806060;
  font-style: italic;
}
</style>
