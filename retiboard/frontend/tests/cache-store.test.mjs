import test from 'node:test'
import assert from 'node:assert/strict'
import { createPinia, setActivePinia } from 'pinia'
import { useCacheStore } from '../src/stores/cacheStore.js'

test('cache store can remove a decrypted payload entry explicitly', () => {
  setActivePinia(createPinia())
  const store = useCacheStore()

  store.set('attachments:blob-a', { attachments: [{ bytes: new Uint8Array([1, 2, 3]) }] })
  assert.equal(store.has('attachments:blob-a'), true)

  assert.equal(store.remove('attachments:blob-a'), true)
  assert.equal(store.has('attachments:blob-a'), false)
  assert.equal(store.get('attachments:blob-a'), null)
})

test('cache store remove is harmless for unknown entries', () => {
  setActivePinia(createPinia())
  const store = useCacheStore()

  assert.equal(store.remove('missing-entry'), false)
})

test('cache store can evict all attachment entries by prefix', () => {
  setActivePinia(createPinia())
  const store = useCacheStore()

  store.set('attachments:blob-a', { attachments: [{ bytes: new Uint8Array([1]) }] })
  store.set('attachments:blob-b', { attachments: [{ bytes: new Uint8Array([2]) }] })
  store.set('text:blob-c', { text: 'hello' })

  assert.equal(store.removeByPrefix('attachments:'), 2)
  assert.equal(store.has('attachments:blob-a'), false)
  assert.equal(store.has('attachments:blob-b'), false)
  assert.equal(store.has('text:blob-c'), true)
})
