/**
 * Decrypted payload cache store (Pinia).
 *
 * Spec §10: "LRU cache for decoded payloads (bounded by max_active_threads_local)."
 * Spec §17: "No browser persistent storage for keys or metadata."
 *
 * This is a simple in-memory LRU cache. Entries are keyed by content_hash.
 * When the cache exceeds either the entry count or byte budget, the
 * least-recently-used entry is evicted. Closing the tab destroys everything.
 */
import { defineStore } from 'pinia'

const MAX_ENTRIES = 100
const MAX_TOTAL_BYTES = 64 * 1024 * 1024

function estimateEntryBytes(payload) {
  if (!payload) return 0
  let total = 0
  if (typeof payload.text === 'string') {
    total += payload.text.length * 2
  }
  if (Array.isArray(payload.attachments)) {
    for (const att of payload.attachments) {
      total += Number(att?.blob?.size || att?.bytes?.byteLength || att?.bytes?.length || 0)
      if (typeof att?.filename === 'string') total += att.filename.length * 2
      if (typeof att?.mime_type === 'string') total += att.mime_type.length * 2
    }
  }
  return total
}

export const useCacheStore = defineStore('cache', () => {
  const _cache = new Map()
  let totalBytes = 0

  function get(contentHash) {
    const entry = _cache.get(contentHash)
    if (!entry) return null
    entry.accessedAt = Date.now()
    return entry.payload
  }

  function set(contentHash, payload) {
    const entryBytes = estimateEntryBytes(payload)
    if (_cache.has(contentHash)) {
      totalBytes -= _cache.get(contentHash).bytes
    }
    _cache.set(contentHash, {
      payload,
      bytes: entryBytes,
      accessedAt: Date.now(),
    })
    totalBytes += entryBytes
    _enforceLimits()
  }

  function has(contentHash) {
    return _cache.has(contentHash)
  }

  function remove(contentHash) {
    if (!_cache.has(contentHash)) return false
    totalBytes -= _cache.get(contentHash).bytes
    _cache.delete(contentHash)
    return true
  }

  function removeByPrefix(prefix) {
    let removed = 0
    for (const key of Array.from(_cache.keys())) {
      if (!String(key).startsWith(prefix)) continue
      totalBytes -= _cache.get(key).bytes
      _cache.delete(key)
      removed += 1
    }
    return removed
  }

  function clear() {
    _cache.clear()
    totalBytes = 0
  }

  function size() {
    return _cache.size
  }

  function byteSize() {
    return totalBytes
  }

  function _enforceLimits() {
    while ((_cache.size > MAX_ENTRIES || totalBytes > MAX_TOTAL_BYTES) && _cache.size > 0) {
      _evictOldest()
    }
  }

  function _evictOldest() {
    let oldestKey = null
    let oldestTime = Infinity
    for (const [key, entry] of _cache) {
      if (entry.accessedAt < oldestTime) {
        oldestTime = entry.accessedAt
        oldestKey = key
      }
    }
    if (oldestKey) {
      totalBytes -= _cache.get(oldestKey).bytes
      _cache.delete(oldestKey)
    }
  }

  return { get, set, has, remove, removeByPrefix, clear, size, byteSize }
})
