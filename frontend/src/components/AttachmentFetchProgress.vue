<script setup>
const props = defineProps({
  progress: {
    type: Object,
    default: null,
  },
  compact: {
    type: Boolean,
    default: false,
  },
})

function clampPercent(value) {
  const num = Number(value ?? 0)
  if (Number.isNaN(num)) return 0
  return Math.max(0, Math.min(100, Math.round(num)))
}

function displayState(progress) {
  const state = progress?.state || 'fetching'
  switch (state) {
    case 'queued':
      return 'Queued'
    case 'fetching':
      return 'Fetching'
    case 'assembling':
      return 'Assembling'
    case 'finalizing':
      return 'Finalizing...'
    case 'complete':
      return 'Complete'
    case 'failed':
      return 'Failed'
    case 'paused':
      return 'Paused'
    case 'cancelled':
      return 'Cancelled'
    case 'stopped':
      return 'Stopped'
    case 'cooldown':
      return 'Cooling down'
    case 'starting':
      return 'Starting'
    default:
      return state
  }
}

function retryMeta(progress) {
  const retryAt = Number(progress?.retry_at || 0)
  if ((progress?.state || '') !== 'cooldown' || retryAt <= 0) return ''
  const seconds = Math.max(0, Math.ceil((retryAt - Date.now()) / 1000))
  if (seconds <= 0) return 'retry available'
  return `retry in ${seconds}s`
}

function displayError(progress) {
  const error = String(progress?.last_error || '').trim()
  if (!error) return ''
  if ((progress?.state || '') === 'paused') return ''

  const normalized = error.toLowerCase()
  if (normalized.includes('paused')) return ''

  return error
}
</script>

<template>
  <div :class="['rb-progress', { compact }]">
    <div class="rb-progress-row">
      <span class="rb-progress-state">{{ displayState(progress) }}</span>
      <span class="rb-progress-percent">{{ clampPercent(progress?.percent_complete) }}%</span>
    </div>

    <div class="rb-progress-track">
      <div class="rb-progress-fill" :style="{ width: `${clampPercent(progress?.percent_complete)}%` }"></div>
    </div>

    <div v-if="!compact" class="rb-progress-meta">
      <span>{{ progress?.stored_chunks || 0 }}/{{ progress?.chunk_count || 0 }} chunks</span>
      <span v-if="progress?.active_requests">· {{ progress.active_requests }} active</span>
      <span v-if="progress?.available_peers">· {{ progress.available_peers }} peers</span>
      <span v-if="progress?.cooled_down_peers">· {{ progress.cooled_down_peers }} cooling</span>
      <span v-if="progress?.resumed_from_persisted" class="rb-progress-resumed">· resumed</span>
    </div>

    <div v-else class="rb-progress-meta compact-meta">
      <span>{{ progress?.stored_chunks || 0 }}/{{ progress?.chunk_count || 0 }}</span>
      <span v-if="progress?.queue_position">· #{{ progress.queue_position }}</span>
      <span v-if="progress?.resumed_from_persisted" class="rb-progress-resumed">· resumed</span>
      <span v-if="retryMeta(progress)">· {{ retryMeta(progress) }}</span>
    </div>

    <div v-if="displayError(progress)" class="rb-progress-error">
      {{ displayError(progress) }}
    </div>
    <div v-else-if="retryMeta(progress)" class="rb-progress-error">
      {{ retryMeta(progress) }}
    </div>
  </div>
</template>

<style scoped>
.rb-progress {
  min-width: 180px;
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}
.rb-progress.compact {
  min-width: 0;
  width: 100%;
}
.rb-progress-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  font-size: 0.72rem;
  color: #8080a0;
}
.rb-progress-state { color: #a0a0c0; }
.rb-progress-percent { color: #c0c0ff; }
.rb-progress-track {
  width: 100%;
  height: 7px;
  border-radius: 999px;
  background: #111126;
  border: 1px solid #2c2c4f;
  overflow: hidden;
}
.rb-progress-fill {
  height: 100%;
  background: linear-gradient(90deg, #5050a0 0%, #8080ff 100%);
  transition: width 0.25s ease;
}
.rb-progress-meta {
  font-size: 0.65rem;
  color: #606080;
  display: flex;
  flex-wrap: wrap;
  gap: 0.25rem;
}
.compact-meta {
  font-size: 0.62rem;
}
.rb-progress-resumed {
  color: #8fb6ff;
}
.rb-progress-error {
  font-size: 0.65rem;
  color: #d07a7a;
}
</style>
