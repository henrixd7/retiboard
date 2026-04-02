/**
 * WebSocket composable for real-time board updates.
 *
 * Connects to /ws/boards/{boardId} and emits events when
 * new structural metadata arrives. The frontend then fetches
 * and decrypts payloads on-demand.
 */
import { ref, onUnmounted } from 'vue'

export function useBoardSocket(boardId) {
  const connected = ref(false)
  const lastEvent = ref(null)
  let ws = null
  let reconnectTimer = null
  let disposed = false
  const listeners = []

  function connect() {
    if (disposed || ws) return
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const url = `${proto}//${location.host}/ws/boards/${boardId}`
    ws = new WebSocket(url)

    ws.onopen = () => { connected.value = true }
    ws.onclose = () => {
      connected.value = false
      ws = null
      if (disposed || reconnectTimer) return
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null
        if (!ws && !disposed) connect()
      }, 5000)
    }
    ws.onerror = () => { ws?.close() }
    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data)
        if (msg.event === 'ping') return
        lastEvent.value = msg
        listeners.forEach(fn => fn(msg))
      } catch { /* ignore malformed */ }
    }
  }

  function onEvent(fn) {
    listeners.push(fn)
  }

  function disconnect() {
    disposed = true
    if (reconnectTimer) {
      clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
    if (ws) { ws.close(); ws = null }
  }

  onUnmounted(disconnect)
  connect()

  return { connected, lastEvent, onEvent, disconnect }
}
