import test from 'node:test'
import assert from 'node:assert/strict'
import { createPinia, setActivePinia } from 'pinia'
import { useModerationStore } from '../src/stores/moderationStore.js'

function makeStorage() {
  const data = new Map()
  return {
    getItem(key) {
      return data.has(key) ? data.get(key) : null
    },
    setItem(key, value) {
      data.set(key, String(value))
    },
    removeItem(key) {
      data.delete(key)
    },
    clear() {
      data.clear()
    },
  }
}

test('moderation store persists global banned file hashes and hidden placeholders locally', () => {
  const previousStorage = globalThis.localStorage
  globalThis.localStorage = makeStorage()

  try {
    setActivePinia(createPinia())
    const store = useModerationStore()

    store.banFileHash('ABC123')
    store.hideBannedFilePlaceholder('abc123')

    assert.equal(store.isFileHashBanned('abc123'), true)
    assert.equal(store.isBannedFilePlaceholderHidden('abc123'), true)

    setActivePinia(createPinia())
    const reloadedStore = useModerationStore()
    assert.equal(reloadedStore.isFileHashBanned('abc123'), true)
    assert.equal(reloadedStore.isBannedFilePlaceholderHidden('abc123'), true)

    reloadedStore.unbanFileHash('abc123')
    assert.equal(reloadedStore.isFileHashBanned('abc123'), false)
    assert.equal(reloadedStore.isBannedFilePlaceholderHidden('abc123'), false)
  } finally {
    globalThis.localStorage = previousStorage
  }
})
