<script setup>
import { toRef } from 'vue'
import { useThreadPostText } from '../composables/useThreadPostText.js'

const props = defineProps({
  text: {
    type: String,
    default: '',
  },
  posts: {
    type: Array,
    default: () => [],
  },
  truncateLimit: {
    type: Number,
    default: 2000,
  },
})

const emit = defineEmits(['scroll-to-post', 'show-post-preview', 'hide-post-preview'])

const {
  expanded,
  isTruncated,
  segments,
  toggleExpand,
} = useThreadPostText({
  text: toRef(props, 'text'),
  posts: toRef(props, 'posts'),
  truncateLimit: toRef(props, 'truncateLimit'),
})

function onScrollToPost(postId) {
  emit('scroll-to-post', postId)
}

function onShowPreview(postId, event) {
  emit('show-post-preview', postId, event)
}

function onHidePreview() {
  emit('hide-post-preview')
}
</script>

<template>
  <div class="post-text">
    <template v-for="(seg, segmentIndex) in segments" :key="segmentIndex">
      <br v-if="seg.type === 'newline'" />
      <span
        v-else-if="seg.type === 'text'"
        :class="{ greentext: seg.quote === 'green', bluetext: seg.quote === 'blue' }"
      >
        {{ seg.value }}
      </span>
      <a
        v-else-if="seg.type === 'ref' && seg.exists"
        class="post-ref"
        href="#"
        @click.prevent.stop="onScrollToPost(seg.postId)"
        @mouseenter="onShowPreview(seg.postId, $event)"
        @mouseleave="onHidePreview"
      >&gt;&gt;{{ seg.id }}</a>
      <span v-else-if="seg.type === 'ref'" class="post-ref dead">&gt;&gt;{{ seg.id }}</span>
      <a
        v-else-if="seg.type === 'url'"
        :href="seg.value"
        target="_blank"
        rel="noopener noreferrer"
        :class="['post-link', { greentext: seg.quote === 'green', bluetext: seg.quote === 'blue' }]"
        @click.stop
      >{{ seg.value }}</a>
    </template>
    <span v-if="isTruncated" class="truncation">
      …
      <button class="expand-btn" @click.stop="toggleExpand">
        Show full text ({{ text.length.toLocaleString() }} chars)
      </button>
    </span>
    <button
      v-else-if="text.length > truncateLimit && expanded"
      class="expand-btn"
      @click.stop="toggleExpand"
    >Collapse</button>
  </div>
</template>

<style scoped>
.post-text {
  flex: 1;
  font-size: 0.85rem;
  line-height: 1.55;
  color: #c0c0d0;
  white-space: pre-wrap;
  word-break: break-word;
}

.post-ref {
  color: #7070ff;
  cursor: pointer;
  text-decoration: none;
  font-family: monospace;
  font-size: 0.8rem;
}

.post-ref:hover {
  color: #a0a0ff;
}

.post-ref.dead {
  color: #505060;
  cursor: default;
  text-decoration: line-through;
}

.post-link {
  color: #60a0ff;
  text-decoration: none;
  word-break: break-all;
  font-size: 0.8rem;
}

.post-link:hover {
  color: #90c0ff;
}

.greentext {
  color: #02af02;
}

.bluetext {
  color: #3381d2;
}

.truncation {
  color: #606080;
}

.expand-btn {
  background: none;
  border: none;
  color: #6060c0;
  cursor: pointer;
  font-family: inherit;
  font-size: 0.75rem;
  padding: 0.1rem 0.3rem;
  text-decoration: underline;
}

.expand-btn:hover {
  color: #a0a0ff;
}
</style>
