import { defineStore } from 'pinia'
import { ref, computed, onUnmounted } from 'vue'
import { apiJson } from '../utils/api.js'

/**
 * Global network & sync status store.
 * Polls the backend status API to provide visibility into
 * RNS path resolution and background synchronization (§10).
 */
export const useNetworkStore = defineStore('network', () => {
  const status = ref(null)
  const lastUpdate = ref(0)
  const isPolling = ref(false)
  let pollTimer = null

  const peerSummary = computed(() => {
    if (!status.value) return { total: 0, known: 0, resolving: 0 }
    const s = status.value.path_summary || {}
    return {
      total: status.value.total_peers || 0,
      known: s.known || 0,
      resolving: (s.requested || 0) + (s.unknown || 0),
      stale: s.stale || 0,
      unreachable: s.unreachable || 0,
    }
  })

  const activeSyncs = computed(() => {
    if (!status.value) return { catchup: [], delta: 0, fetches: [] }
    return {
      catchup: status.value.active_sync_tasks?.catchup_boards || [],
      delta: status.value.active_sync_tasks?.delta_queue_size || 0,
      fetches: status.value.active_fetches || [],
    }
  })

  const isSyncing = computed(() => {
    const s = activeSyncs.value
    return s.catchup.length > 0 || s.delta > 0 || s.fetches.length > 0
  })

  const isLowBandwidth = computed(() => !!status.value?.is_low_bandwidth)

  async function updateStatus() {
    try {
      const data = await apiJson('/api/status')
      status.value = data
      lastUpdate.value = Date.now()
    } catch (err) {
      console.error('Failed to update network status:', err)
    }
  }

  function startPolling(intervalMs = 5000) {
    if (isPolling.value) return
    isPolling.value = true
    updateStatus()
    pollTimer = setInterval(updateStatus, intervalMs)
  }

  function stopPolling() {
    isPolling.value = false
    if (pollTimer) {
      clearInterval(pollTimer)
      pollTimer = null
    }
  }

  onUnmounted(stopPolling)

  return {
    status,
    lastUpdate,
    peerSummary,
    activeSyncs,
    isSyncing,
    isLowBandwidth,
    updateStatus,
    startPolling,
    stopPolling,
  }
})
