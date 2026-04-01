import test from 'node:test'
import assert from 'node:assert/strict'
import { createPinia, setActivePinia } from 'pinia'
import { useAttachmentTracker } from '../src/stores/attachmentTracker.js'

test('attachment tracker forgets a downloaded blob hash explicitly', () => {
  setActivePinia(createPinia())
  const store = useAttachmentTracker()

  store.markDownloaded('blob-a')
  assert.equal(store.isDownloaded('blob-a'), true)

  store.forgetDownloaded('blob-a')
  assert.equal(store.isDownloaded('blob-a'), false)
})

test('attachment tracker forget is safe for unknown hashes and allows re-marking', () => {
  setActivePinia(createPinia())
  const store = useAttachmentTracker()

  store.forgetDownloaded('missing-hash')
  assert.equal(store.isDownloaded('missing-hash'), false)

  store.markDownloaded('blob-b')
  store.forgetDownloaded('blob-b')
  store.markDownloaded('blob-b')
  assert.equal(store.isDownloaded('blob-b'), true)
})
