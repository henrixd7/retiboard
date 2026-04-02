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
const DEFAULT_SHOW_SUBSCRIBED_BOARD_LINKS = true

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

function sanitizeStringArray(value) {
  if (!Array.isArray(value)) return []

  const seen = new Set()
  const normalized = []
  for (const item of value) {
    if (typeof item !== 'string') continue
    const key = item.trim()
    if (!key || seen.has(key)) continue
    seen.add(key)
    normalized.push(key)
  }
  return normalized
}

function arraysEqual(left, right) {
  if (left.length !== right.length) return false
  return left.every((value, index) => value === right[index])
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
  const showSubscribedBoardLinks = ref(DEFAULT_SHOW_SUBSCRIBED_BOARD_LINKS)
  const subscribedBoardOrder = ref([])
  const quickLinkBoardIds = ref(null)

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
    void saveBackendSettings({
      pinned_threads: [...pinnedThreads.value],
    })
  }

  function unpinThread(boardId, threadId) {
    const next = new Set(pinnedThreads.value)
    next.delete(`${boardId}:${threadId}`)
    pinnedThreads.value = next
    persistPins()
    void saveBackendSettings({
      pinned_threads: [...pinnedThreads.value],
    })
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
      if (Array.isArray(parsed)) pinnedThreads.value = new Set(sanitizeStringArray(parsed))
    } catch {}
  }

  function replacePinnedThreads(threadKeys) {
    pinnedThreads.value = new Set(sanitizeStringArray(threadKeys))
    persistPins()
  }

  function buildOrderedBoardIds(availableBoardIds) {
    const available = sanitizeStringArray(availableBoardIds)
    const availableSet = new Set(available)
    const ordered = subscribedBoardOrder.value.filter((boardId) => availableSet.has(boardId))

    for (const boardId of available) {
      if (!ordered.includes(boardId)) ordered.push(boardId)
    }
    return ordered
  }

  function reconcileBoardPreferences(boards) {
    const availableBoardIds = sanitizeStringArray(
      (boards || []).map((board) => board?.board_id).filter(Boolean),
    )
    const nextOrder = buildOrderedBoardIds(availableBoardIds)
    if (!arraysEqual(subscribedBoardOrder.value, nextOrder)) {
      subscribedBoardOrder.value = nextOrder
    }

    if (quickLinkBoardIds.value !== null) {
      const availableSet = new Set(availableBoardIds)
      const nextVisible = quickLinkBoardIds.value.filter((boardId) => availableSet.has(boardId))
      if (!arraysEqual(quickLinkBoardIds.value, nextVisible)) {
        quickLinkBoardIds.value = nextVisible
      }
    }
  }

  function setSubscribedBoardOrder(boardIds) {
    subscribedBoardOrder.value = sanitizeStringArray(boardIds)
  }

  function setQuickLinkBoardIds(boardIds) {
    if (boardIds === null) {
      quickLinkBoardIds.value = null
      return
    }
    quickLinkBoardIds.value = sanitizeStringArray(boardIds)
  }

  function moveSubscribedBoard(boardId, targetIndex) {
    const ordered = [...subscribedBoardOrder.value]
    const fromIndex = ordered.indexOf(boardId)
    if (fromIndex === -1) return

    const clampedTargetIndex = Math.max(0, Math.min(targetIndex, ordered.length - 1))
    if (fromIndex === clampedTargetIndex) return

    ordered.splice(fromIndex, 1)
    ordered.splice(clampedTargetIndex, 0, boardId)
    subscribedBoardOrder.value = ordered
  }

  function getOrderedBoards(boards) {
    const boardById = new Map((boards || []).map((board) => [board.board_id, board]))
    return buildOrderedBoardIds((boards || []).map((board) => board.board_id))
      .map((boardId) => boardById.get(boardId))
      .filter(Boolean)
  }

  function getQuickLinkBoards(boards) {
    if (!showSubscribedBoardLinks.value) return []

    const orderedBoards = getOrderedBoards(boards)
    if (quickLinkBoardIds.value === null) return orderedBoards

    const visibleBoardIds = new Set(quickLinkBoardIds.value)
    return orderedBoards.filter((board) => visibleBoardIds.has(board.board_id))
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
      show_subscribed_board_links: Boolean(showSubscribedBoardLinks.value),
      subscribed_board_order: sanitizeStringArray(subscribedBoardOrder.value),
      quick_link_board_ids: quickLinkBoardIds.value === null
        ? null
        : sanitizeStringArray(quickLinkBoardIds.value),
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
        const backendPinnedThreads = sanitizeStringArray(data.pinned_threads)
        const mergedPinnedThreads = sanitizeStringArray([
          ...pinnedThreads.value,
          ...backendPinnedThreads,
        ])
        replacePinnedThreads(mergedPinnedThreads)
        if (mergedPinnedThreads.length !== backendPinnedThreads.length) {
          void saveBackendSettings({
            pinned_threads: mergedPinnedThreads,
          })
        }
      }
    } catch (e) {
      consoleStore.pushLog(`Failed to fetch backend settings: ${e.message}`, 'ERROR')
    }
  }

  async function saveBackendSettings(settingsDelta = null) {
    if (typeof window === 'undefined' || !window.location || !window.location.host) return
    try {
      const settings = settingsDelta || {
        global_storage_limit_mb: globalStorageLimitMB.value,
      }
      await fetch('/api/settings', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          settings,
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
      showSubscribedBoardLinks.value = typeof parsed.show_subscribed_board_links === 'boolean'
        ? parsed.show_subscribed_board_links
        : DEFAULT_SHOW_SUBSCRIBED_BOARD_LINKS
      subscribedBoardOrder.value = sanitizeStringArray(parsed.subscribed_board_order)
      quickLinkBoardIds.value = Object.prototype.hasOwnProperty.call(parsed, 'quick_link_board_ids')
        ? (parsed.quick_link_board_ids === null
            ? null
            : sanitizeStringArray(parsed.quick_link_board_ids))
        : null
    } catch {}
  }

  hydrateSettings()
  hydratePins()
  fetchBackendSettings()

  watch([
    maxAutoRenderBytes,
    maxManualLoadBytes,
    showSubscribedBoardLinks,
    subscribedBoardOrder,
    quickLinkBoardIds,
  ], () => {
    persistSettings()
  })

  watch(globalStorageLimitMB, () => {
    void saveBackendSettings({
      global_storage_limit_mb: globalStorageLimitMB.value,
    })
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
    showSubscribedBoardLinks, subscribedBoardOrder, quickLinkBoardIds,
    pinnedThreads, pinThread, unpinThread, isThreadPinned, replacePinnedThreads,
    setSubscribedBoardOrder, setQuickLinkBoardIds, moveSubscribedBoard,
    reconcileBoardPreferences, getOrderedBoards, getQuickLinkBoards,
    prettySize, shouldAutoRender,
    fetchBackendSettings, saveBackendSettings
  }
})
