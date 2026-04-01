/**
 * User settings store (Pinia).
 *
 * Non-secret UI/fetch preferences are persisted browser-locally so they
 * survive page reloads and browser restarts, while cryptographic key
 * material remains memory-only.
 */
import { defineStore } from 'pinia'
import { ref, watch } from 'vue'
import { useConsoleStore } from './consoleStore.js'

const STORAGE_KEY = 'retiboard:settings:v1'
const PINS_STORAGE_KEY = 'retiboard:pins:v1'
const DEFAULT_MAX_AUTO_RENDER_BYTES = 512 * 1024
const DEFAULT_MAX_MANUAL_LOAD_BYTES = 0

function getStorage() {
  if (typeof window !== 'undefined' && window?.localStorage) {
    return window.localStorage
  }
  if (typeof globalThis !== 'undefined' && globalThis?.localStorage) {
    return globalThis.localStorage
  }
  return null
}

function sanitizeNonNegativeInteger(value, fallback) {
  const parsed = Number(value)
  if (!Number.isFinite(parsed) || parsed < 0) return fallback
  return Math.round(parsed)
}

export const useSettingsStore = defineStore('settings', () => {
  const consoleStore = useConsoleStore()
  /**
   * Max attachment payload size (bytes) to auto-fetch and render.
   *  0  = never auto-load files
   * >0  = auto-load if the encrypted attachment blob is within this size
   *
   * Default: 524288 (512 KB).
   */
  const maxAutoRenderBytes = ref(DEFAULT_MAX_AUTO_RENDER_BYTES)
  // Optional local hard cap for manual loads. 0 = disabled (default).
  const maxManualLoadBytes = ref(DEFAULT_MAX_MANUAL_LOAD_BYTES)

  /**
   * Backend global settings (synchronized).
   */
  const globalStorageLimitMB = ref(1024)

  /**
   * Locally pinned threads. Stored as a flat Set of "boardId:threadId" keys
   * so it is reactive-safe (replaced on mutation) and board-namespaced.
   * Pinned threads are sorted first in the catalog and never auto-purged.
   */
  const pinnedThreads = ref(new Set())

  function pinThread(boardId, threadId) {
    const next = new Set(pinnedThreads.value)
    next.add(`${boardId}:${threadId}`)
    pinnedThreads.value = next
    persistPins()
  }

  function unpinThread(boardId, threadId) {
    const next = new Set(pinnedThreads.value)
    next.delete(`${boardId}:${threadId}`)
    pinnedThreads.value = next
    persistPins()
  }

  function isThreadPinned(boardId, threadId) {
    return pinnedThreads.value.has(`${boardId}:${threadId}`)
  }

  function persistPins() {
    const storage = getStorage()
    if (!storage) return
    try {
      storage.setItem(PINS_STORAGE_KEY, JSON.stringify([...pinnedThreads.value]))
    } catch {}
  }

  function hydratePins() {
    const storage = getStorage()
    if (!storage) return
    try {
      const raw = storage.getItem(PINS_STORAGE_KEY)
      if (!raw) return
      const parsed = JSON.parse(raw)
      if (Array.isArray(parsed)) pinnedThreads.value = new Set(parsed)
    } catch {}
  }

  function persistSettings() {
    const storage = getStorage()
    if (!storage) return

    const payload = {
      max_auto_render_bytes: sanitizeNonNegativeInteger(
        maxAutoRenderBytes.value,
        DEFAULT_MAX_AUTO_RENDER_BYTES
      ),
      max_manual_load_bytes: sanitizeNonNegativeInteger(
        maxManualLoadBytes.value,
        DEFAULT_MAX_MANUAL_LOAD_BYTES
      ),
    }

    try {
      storage.setItem(STORAGE_KEY, JSON.stringify(payload))
    } catch {}
  }

  async function fetchBackendSettings() {
    if (typeof window === 'undefined' || !window.location || !window.location.host) return
    try {
      const res = await fetch('/api/settings')
      if (res.ok) {
        const data = await res.json()
        globalStorageLimitMB.value = data.global_storage_limit_mb
      }
    } catch (e) {
      consoleStore.pushLog(`Failed to fetch backend settings: ${e.message}`, 'ERROR')
    }
  }

  async function saveBackendSettings() {
    if (typeof window === 'undefined' || !window.location || !window.location.host) return
    try {
      await fetch('/api/settings', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          settings: {
            global_storage_limit_mb: globalStorageLimitMB.value
          }
        })
      })
    } catch (e) {
      consoleStore.pushLog(`Failed to save backend settings: ${e.message}`, 'ERROR')
    }
  }

  function hydrateSettings() {
    const storage = getStorage()
    if (!storage) return

    try {
      const raw = storage.getItem(STORAGE_KEY)
      if (!raw) return
      const parsed = JSON.parse(raw) || {}
      maxAutoRenderBytes.value = sanitizeNonNegativeInteger(
        parsed.max_auto_render_bytes,
        DEFAULT_MAX_AUTO_RENDER_BYTES
      )
      maxManualLoadBytes.value = sanitizeNonNegativeInteger(
        parsed.max_manual_load_bytes,
        DEFAULT_MAX_MANUAL_LOAD_BYTES
      )
    } catch {}
  }

  hydrateSettings()
  hydratePins()
  fetchBackendSettings()

  watch([maxAutoRenderBytes, maxManualLoadBytes], () => {
    persistSettings()
  })

  watch(globalStorageLimitMB, () => {
    saveBackendSettings()
  })

  function prettySize(bytes) {
    if (bytes <= 0) return '0 B'
    if (bytes < 1024) return `${bytes} B`
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
    if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
    return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`
  }

  function shouldAutoRender(attachmentPayloadSize) {
    if (maxAutoRenderBytes.value <= 0) return false
    return attachmentPayloadSize <= maxAutoRenderBytes.value
  }

  return {
    maxAutoRenderBytes, maxManualLoadBytes, globalStorageLimitMB,
    pinnedThreads, pinThread, unpinThread, isThreadPinned,
    prettySize, shouldAutoRender,
    fetchBackendSettings, saveBackendSettings
  }
})
