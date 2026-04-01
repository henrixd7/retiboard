<script setup>
import { ref, computed, onMounted, onUnmounted, watch, nextTick } from 'vue'
import { useConsoleStore } from '../stores/consoleStore'

const consoleStore = useConsoleStore()
const consoleEl = ref(null)
const pollInterval = ref(null)
const filter = ref('ALL')

const filteredLogs = computed(() => {
  if (filter.value === 'ALL') return consoleStore.logs
  if (filter.value === 'ERRORS') return consoleStore.logs.filter(l => l.level === 'ERROR' || l.level === 'CRITICAL')
  if (filter.value === 'RNS') return consoleStore.logs.filter(l => l.name?.toLowerCase().includes('rns') || l.name?.toLowerCase().includes('sync'))
  return consoleStore.logs
})

const scrollToBottom = async () => {
  await nextTick()
  if (consoleEl.value) {
    consoleEl.value.scrollTop = consoleEl.value.scrollHeight
  }
}

const formatTimestamp = (ts) => {
  return new Date(ts * 1000).toLocaleTimeString([], { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

const getLogLevelColor = (log) => {
  const l = log.level?.toUpperCase() || ''
  const name = log.name?.toLowerCase() || ''
  if (l === 'ERROR' || l === 'CRITICAL') return '#ff003c' // Brutal Red
  if (l === 'WARNING' || l === 'WARN') return '#f3ff00'  // Amber
  if (name.includes('rns') || name.includes('system') || name.includes('sync')) return '#39ff14' // Matrix Green
  return '#00f3ff' // Cyan (Info)
}

onMounted(() => {
  consoleStore.fetchBackendLogs()
  pollInterval.value = setInterval(() => {
    consoleStore.fetchBackendLogs().then(() => {
      if (consoleStore.isExpanded) {
        scrollToBottom()
      }
    })
  }, 4000)
})

onUnmounted(() => {
  if (pollInterval.value) clearInterval(pollInterval.value)
})

watch(() => consoleStore.logs.length, () => {
  if (consoleStore.isExpanded) {
    scrollToBottom()
  }
})

watch(() => consoleStore.isExpanded, (expanded) => {
  if (expanded) {
    scrollToBottom()
  }
})
</script>

<template>
  <div class="system-hud-container">
    
    <!-- Header Bar (Ticker) -->
    <div class="hud-header" @click.self="consoleStore.toggle">
      <div class="hud-center-constrain">
        <div class="hud-ticker">
          <span class="hud-prompt">&gt;</span>
          <div v-if="consoleStore.latestLog" class="hud-latest-entry truncate">
            <span class="hud-ts">[{{ formatTimestamp(consoleStore.latestLog.timestamp) }}]</span>
            <span class="hud-lvl" :style="{ color: getLogLevelColor(consoleStore.latestLog) }">
              [{{ consoleStore.latestLog.level }}]
            </span>
            <span class="hud-msg">{{ consoleStore.latestLog.message }}</span>
          </div>
          <div v-else class="hud-latest-entry italic text-gray-600">
            SYSTEM_READY // NODE_IDLE
          </div>
        </div>

        <div class="hud-controls">
          <!-- Filters -->
          <div v-if="consoleStore.isExpanded" class="hud-filters">
            <button @click="filter = 'ALL'" class="hud-filter-btn" :class="{ active: filter === 'ALL' }">ALL</button>
            <button @click="filter = 'RNS'" class="hud-filter-btn" :class="{ active: filter === 'RNS' }">RNS</button>
            <button @click="filter = 'ERRORS'" class="hud-filter-btn" :class="{ active: filter === 'ERRORS' }">ERRORS</button>
          </div>

          <div v-if="(consoleStore.hasNewActivity || consoleStore.hasError) && !consoleStore.isExpanded" 
               class="hud-activity-dot"
               :class="{ 'is-error': consoleStore.hasError }"></div>
          <button @click="consoleStore.clear" class="hud-btn" title="Clear Console">CLEAR</button>
          <button @click="consoleStore.toggle" class="hud-toggle-btn" :class="{ 'is-open': consoleStore.isExpanded }">
            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="square" stroke-linejoin="miter"><path d="m18 15-6-6-6 6"/></svg>
          </button>
        </div>
      </div>
    </div>

    <!-- Log Drawer (Shell) -->
    <div v-show="consoleStore.isExpanded" class="hud-drawer">
      <div class="hud-center-constrain">
        <div ref="consoleEl" class="hud-log-area">
          <div v-for="log in filteredLogs" :key="log.id" class="hud-log-line">
            <span class="hud-ts">[{{ formatTimestamp(log.timestamp) }}]</span>
            <span class="hud-lvl" :style="{ color: getLogLevelColor(log) }">[{{ log.level }}]</span>
            <span class="hud-msg">{{ log.message }}</span>
          </div>
        </div>
      </div>
    </div>

  </div>
</template>

<style scoped>
.system-hud-container {
  position: fixed;
  bottom: 0;
  left: 0;
  width: 100%;
  z-index: 9999;
  background: rgba(5, 5, 10, 0.95);
  border-top: 2px solid #2a2a4a;
  font-family: 'Fira Code', 'JetBrains Mono', 'Courier New', monospace;
}

.hud-header {
  height: 32px;
  display: flex;
  align-items: center;
  border-bottom: 2px solid #2a2a4a;
  cursor: pointer;
  user-select: none;
}

.hud-center-constrain {
  width: 100%;
  max-width: 80ch; /* Standard terminal width */
  margin: 0 auto;
  display: flex;
  align-items: center;
  height: 100%;
  padding: 0 1rem;
}

.hud-ticker {
  flex: 1;
  display: flex;
  align-items: center;
  gap: 0.75rem;
  min-width: 0;
  font-size: 11px;
}

.hud-prompt {
  color: #39ff14;
  font-weight: bold;
}

.hud-latest-entry {
  display: flex;
  gap: 0.5rem;
  overflow: hidden;
  white-space: nowrap;
}

.hud-ts { color: #505070; }
.hud-lvl { font-weight: bold; min-width: 5.5ch; }
.hud-msg { color: #c0c0d0; }

.hud-controls {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  margin-left: 1rem;
}

.hud-filters {
  display: flex;
  gap: 0.4rem;
  border-right: 1px solid #2a2a4a;
  padding-right: 0.75rem;
  margin-right: 0.25rem;
}

.hud-filter-btn {
  background: none;
  border: 1px solid #2a2a4a;
  color: #505070;
  font-size: 9px;
  padding: 1px 4px;
  cursor: pointer;
  letter-spacing: 0.05em;
}

.hud-filter-btn:hover { color: #a0a0ff; border-color: #3a3a5a; }
.hud-filter-btn.active { color: #00f3ff; border-color: #00f3ff; background: rgba(0, 243, 255, 0.05); }

.hud-activity-dot {
  width: 6px;
  height: 6px;
  background: #f3ff00;
  box-shadow: 0 0 8px #f3ff00;
  animation: blip 0.5s ease-out;
}

.hud-activity-dot.is-error {
  background: #ff003c;
  box-shadow: 0 0 10px #ff003c;
  animation: pulse-red 1.5s infinite;
}

@keyframes pulse-red {
  0% { transform: scale(1); opacity: 1; }
  50% { transform: scale(1.3); opacity: 0.6; }
  100% { transform: scale(1); opacity: 1; }
}

@keyframes blip {
  0% { transform: scale(0.5); opacity: 0; }
  20% { transform: scale(1.5); opacity: 1; }
  100% { transform: scale(1); opacity: 1; }
}

.hud-btn {
  background: none;
  border: 1px solid #3a3a5a;
  color: #606080;
  font-size: 9px;
  padding: 1px 4px;
  cursor: pointer;
  letter-spacing: 0.1em;
}

.hud-btn:hover {
  background: #1a1a3a;
  color: #a0a0ff;
  border-color: #5050a0;
}

.hud-toggle-btn {
  background: none;
  border: none;
  color: #505070;
  cursor: pointer;
  display: flex;
  align-items: center;
  transition: transform 0.2s, color 0.2s;
}

.hud-toggle-btn:hover { color: #a0a0ff; }
.hud-toggle-btn.is-open { transform: rotate(180deg); }

.hud-drawer {
  max-height: 250px;
  border-top: none;
  padding-bottom: 0.5rem;
}

.hud-log-area {
  height: 250px;
  overflow-y: auto;
  padding: 0.75rem 0;
  font-size: 11px;
  line-height: 1.4;
  scrollbar-width: thin;
  scrollbar-color: #3a3a5a transparent;
}

.hud-log-line {
  display: flex;
  gap: 0.75rem;
  padding: 1px 0;
}

.hud-log-area::-webkit-scrollbar {
  width: 4px;
}
.hud-log-area::-webkit-scrollbar-track {
  background: transparent;
}
.hud-log-area::-webkit-scrollbar-thumb {
  background: #2a2a4a;
}
</style>
