/**
 * Board store (Pinia) — manages board list and current board state.
 *
 * Spec §10: "Board key held in memory only (re-derived per session)."
 * Spec §17: "No browser persistent storage for keys or metadata."
 *
 * key_material is fetched from the API and held in this store's
 * reactive state. It is NEVER written to localStorage or IndexedDB.
 * Closing the tab loses it. Re-opening re-fetches from API.
 */
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { deriveBoardKey } from '../crypto/boardKey.js'
import { apiJson, apiRequest } from '../utils/api.js'

export const useBoardStore = defineStore('boards', () => {
  const boards = ref([])
  const discoveredBoards = ref([])
  const discoveredAdvisoryOrder = ref([])
  const discoveredStaleAfterSeconds = ref(0)
  const currentBoardId = ref(null)
  const loading = ref(false)
  const error = ref(null)

  // Derived board keys: { boardId: CryptoKey } — in memory only.
  const _boardKeys = new Map()

  const currentBoard = computed(() =>
    boards.value.find(b => b.board_id === currentBoardId.value) || null
  )

  async function fetchBoards() {
    loading.value = true
    error.value = null
    try {
      const data = await apiJson('/api/boards')
      boards.value = data.boards || []
    } catch (e) {
      error.value = e.message
    } finally {
      loading.value = false
    }
  }

  async function fetchDiscovered() {
    try {
      const { response, data } = await apiRequest('/api/boards/discovered', {
        parseAs: 'json',
        throwOnError: false,
      })
      if (!response?.ok) return
      discoveredBoards.value = data.boards || []
      discoveredAdvisoryOrder.value = data.advisory_order || []
      discoveredStaleAfterSeconds.value = data.stale_after_seconds || 0
    } catch { /* ignore */ }
  }

  async function createBoard(params) {
    const board = await apiJson('/api/boards', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params),
    })
    boards.value.push(board)
    return board
  }

  async function subscribeToBoard(boardId) {
    const board = await apiJson(`/api/boards/${boardId}/subscribe`, {
      method: 'POST',
    })
    boards.value.push(board)
    // Remove from discovered.
    discoveredBoards.value = discoveredBoards.value.filter(
      b => b.board_id !== boardId
    )
    await fetchDiscovered()
    return board
  }

  async function unsubscribe(boardId) {
    // Capture board data before removing so we can move it to discovered.
    const board = boards.value.find(b => b.board_id === boardId)

    await apiRequest(`/api/boards/${boardId}`, {
      method: 'DELETE',
      parseAs: 'response',
    })
    boards.value = boards.value.filter(b => b.board_id !== boardId)
    _boardKeys.delete(boardId)
    if (currentBoardId.value === boardId) currentBoardId.value = null

    // Refresh discovered metadata from the backend so the entry includes
    // freshness and advisory telemetry immediately after unsubscribe.
    if (board) await fetchDiscovered()
  }

  /**
   * Get or derive the AES-GCM key for a board.
   * Key stays in memory — never persisted (§10, §17).
   */
  async function getBoardKey(boardId) {
    if (_boardKeys.has(boardId)) return _boardKeys.get(boardId)

    const board = boards.value.find(b => b.board_id === boardId)
    if (!board || !board.key_material) {
      throw new Error('Board key_material not available')
    }

    const key = await deriveBoardKey(board.key_material, boardId)
    _boardKeys.set(boardId, key)
    return key
  }

  function selectBoard(boardId) {
    currentBoardId.value = boardId
  }

  return {
    boards, discoveredBoards, discoveredAdvisoryOrder,
    discoveredStaleAfterSeconds, currentBoardId, currentBoard,
    loading, error,
    fetchBoards, fetchDiscovered, createBoard, subscribeToBoard,
    unsubscribe, getBoardKey, selectBoard,
  }
})
