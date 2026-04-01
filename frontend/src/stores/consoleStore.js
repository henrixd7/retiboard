import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { apiJson } from '../utils/api.js'

export const useConsoleStore = defineStore('console', () => {
  const logs = ref([])
  const isExpanded = ref(false)
  const hasNewActivity = ref(false)
  const activityTimeout = ref(null)
  const hasError = ref(false)
  const lastSeenTimestamp = ref(0)

  const STORAGE_KEY = 'retiboard:console:logs'
  const MAX_LOGS = 200

  function persistLogs() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(logs.value.slice(-100)))
    } catch (e) {}
  }

  function hydrateLogs() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY)
      if (raw) {
        const parsed = JSON.parse(raw)
        if (Array.isArray(parsed)) {
          logs.value = parsed
          if (logs.value.length > 0) {
            lastSeenTimestamp.value = Math.max(...logs.value.map(l => l.timestamp))
          }
        }
      }
    } catch (e) {}
  }

  const latestLog = computed(() => {
    return logs.value.length > 0 ? logs.value[logs.value.length - 1] : null
  })

  function triggerActivityBlip(level = 'INFO') {
    const isError = level === 'ERROR' || level === 'CRITICAL'
    
    if (isError) {
      hasError.value = true
    } else if (!hasError.value) {
      // Only trigger yellow blip if there isn't already a persistent red error
      hasNewActivity.value = true
      if (activityTimeout.value) clearTimeout(activityTimeout.value)
      activityTimeout.value = setTimeout(() => {
        hasNewActivity.value = false
        activityTimeout.value = null
      }, 1000)
    }
  }

  function pushLog(message, level = 'INFO') {
    const logEntry = {
      id: Date.now() + Math.random(),
      timestamp: Date.now() / 1000,
      level: level.toUpperCase(),
      message: message,
      name: 'frontend'
    }
    
    logs.value.push(logEntry)
    if (logs.value.length > MAX_LOGS) {
      logs.value.shift()
    }

    if (!isExpanded.value) {
      triggerActivityBlip(logEntry.level)
    }
    
    persistLogs()
    return logEntry
  }

  async function fetchBackendLogs() {
    if (typeof window === 'undefined' || !window.location || !window.location.host) return []
    try {
      const data = await apiJson('/api/logs')
      if (data && Array.isArray(data)) {
        if (data.length > 0) {
          // Create a set of existing IDs for fast lookup
          const existingIds = new Set(logs.value.map(l => l.id))
          let addedCount = 0

          data.forEach(entry => {
            if (!existingIds.has(entry.id)) {
              logs.value.push(entry)
              addedCount++
              
              if (!isExpanded.value) {
                triggerActivityBlip(entry.level)
              }
              
              if (entry.timestamp > lastSeenTimestamp.value) {
                lastSeenTimestamp.value = entry.timestamp
              }
            }
          })
          
          if (addedCount > 0) {
            if (logs.value.length > MAX_LOGS) {
              logs.value = logs.value.slice(-MAX_LOGS)
            }
            persistLogs()
          }
        }
        return data
      }
    } catch (e) {
      if (e.status !== 401) {
        pushLog(`Failed to fetch system logs: ${e.message}`, 'ERROR')
      }
    }
    return []
  }

  hydrateLogs()

  function toggle() {
    isExpanded.value = !isExpanded.value
    if (isExpanded.value) {
      hasNewActivity.value = false
      hasError.value = false
      if (activityTimeout.value) {
        clearTimeout(activityTimeout.value)
        activityTimeout.value = null
      }
    }
  }

  function clear() {
    logs.value = []
    hasNewActivity.value = false
    hasError.value = false
    persistLogs()
  }

  return {
    logs,
    isExpanded,
    hasNewActivity,
    latestLog,
    pushLog,
    fetchBackendLogs,
    toggle,
    clear
  }
})
