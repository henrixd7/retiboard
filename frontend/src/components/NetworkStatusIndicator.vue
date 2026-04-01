<script setup>
import { onMounted } from 'vue'
import { useNetworkStore } from '../stores/networkStore.js'
import { useConsoleStore } from '../stores/consoleStore.js'

const network = useNetworkStore()
const consoleStore = useConsoleStore()

onMounted(() => {
  network.startPolling(5000)
})

function triggerGossipHook() {
  consoleStore.pushLog('PEER_GOSSIP_INITIATED', 'INFO')
}

function formatBytes(bytes) {
  if (!bytes) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i]
}
</script>

<template>
  <div class="network-status-indicator" v-if="network.status">
    <!-- Connectivity Overview -->
    <div class="status-section peers" 
         @click="triggerGossipHook"
         style="cursor: pointer;"
         :title="`${network.peerSummary.known} known paths, ${network.peerSummary.resolving} resolving`" 
         :class="{ 'has-peers': network.peerSummary.known > 0 }">
      <span class="icon">🌐</span>
      <span class="count">{{ network.peerSummary.known }}</span>
      <span class="label">peers</span>
      
      <div class="path-states" v-if="network.peerSummary.resolving > 0">
        <span class="resolving-dot"></span>
        <span class="resolving-count">{{ network.peerSummary.resolving }}</span>
      </div>
    </div>

    <!-- Active Sync Tasks (HAVE/DELTA) -->
    <div class="status-section sync" v-if="network.isSyncing" title="Active background synchronization">
      <div class="sync-spinner"></div>
      
      <div class="sync-details">
        <span v-if="network.activeSyncs.catchup.length" class="tag catchup">
          Syncing {{ network.activeSyncs.catchup.length }} board(s)
        </span>
        <span v-if="network.activeSyncs.delta > 0" class="tag delta">
          {{ network.activeSyncs.delta }} metadata requests
        </span>
        <span v-if="network.activeSyncs.fetches.length > 0" class="tag fetch">
          Fetching {{ network.activeSyncs.fetches.length }} file(s)
        </span>
      </div>
    </div>

  </div>
</template>

<style scoped>
.network-status-indicator {
  display: flex;
  align-items: center;
  gap: 1rem;
  padding: 0.25rem 0.75rem;
  background: rgba(0, 0, 0, 0.2);
  border-radius: 4px;
  font-size: 0.85rem;
  color: #ccc;
  user-select: none;
}

.status-section {
  display: flex;
  align-items: center;
  gap: 0.4rem;
}

.peers.has-peers {
  color: #4caf50;
}

.path-states {
  display: flex;
  align-items: center;
  gap: 0.2rem;
  font-size: 0.75rem;
  color: #ff9800;
  margin-left: 0.2rem;
}

.resolving-dot {
  width: 6px;
  height: 6px;
  background: #ff9800;
  border-radius: 50%;
  animation: pulse 1.5s infinite;
}

.sync {
  padding-left: 0.5rem;
  border-left: 1px solid #444;
}

.sync-spinner {
  width: 12px;
  height: 12px;
  border: 2px solid #2196f3;
  border-top-color: transparent;
  border-radius: 50%;
  animation: spin 1s linear infinite;
}

.sync-details {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.tag {
  font-size: 0.7rem;
  padding: 0 4px;
  border-radius: 3px;
  background: rgba(255, 255, 255, 0.1);
  white-space: nowrap;
}

.tag.catchup { color: #8bc34a; }
.tag.delta { color: #03a9f4; }
.tag.fetch { color: #e91e63; }

.transport {
  margin-left: auto;
  font-size: 0.75rem;
  opacity: 0.7;
}

.transport.low-bw {
  color: #ffeb3b;
  opacity: 1;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

@keyframes pulse {
  0% { opacity: 0.4; }
  50% { opacity: 1; }
  100% { opacity: 0.4; }
}
</style>
