<script setup>
const props = defineProps({
  compact: {
    type: Boolean,
    default: false,
  },
  textOnly: {
    type: Boolean,
    default: false,
  },
})
</script>

<template>
  <div class="empty-attachment" :class="{ compact }">
    <svg class="empty-attachment-icon" viewBox="0 0 64 64" aria-hidden="true">
      <rect x="10" y="12" width="44" height="40" rx="3" />
      <path d="M19 40l9-9 7 7 10-11 8 13" />
      <circle cx="24" cy="24" r="4" />
      <path d="M18 48h28" />
    </svg>
    <div class="empty-attachment-copy">
      <span class="empty-attachment-title">{{ textOnly ? 'Text only' : 'No attachment' }}</span>
      <span class="empty-attachment-subtitle">
        {{ textOnly ? 'This board does not allow files.' : 'Post body only.' }}
      </span>
    </div>
  </div>
</template>

<style scoped>
.empty-attachment {
  width: 100%;
  min-height: 170px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0.55rem;
  padding: 0.9rem;
  border: 1px dashed #30405a;
  border-radius: 4px;
  background:
    linear-gradient(180deg, rgba(20, 25, 42, 0.98) 0%, rgba(13, 17, 30, 0.98) 100%),
    repeating-linear-gradient(
      -45deg,
      rgba(98, 120, 164, 0.08) 0,
      rgba(98, 120, 164, 0.08) 8px,
      transparent 8px,
      transparent 16px
    );
  color: #91a2cb;
}

.empty-attachment.compact {
  aspect-ratio: 4 / 3;
  min-height: 0;
  border-radius: 0;
  border-left: none;
  border-right: none;
  border-top: none;
}

.empty-attachment-icon {
  width: 2.6rem;
  height: 2.6rem;
  fill: none;
  stroke: currentColor;
  stroke-width: 2;
  opacity: 0.8;
}

.empty-attachment-copy {
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
  text-align: center;
}

.empty-attachment-title {
  font-size: 0.76rem;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.empty-attachment-subtitle {
  font-size: 0.67rem;
  color: #66779e;
}
</style>
