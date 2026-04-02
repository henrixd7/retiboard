import test from 'node:test'
import assert from 'node:assert/strict'
import { __attachmentLoaderInternals } from '../src/composables/useAttachmentLoader.js'

test('attachment auto-fetch governor queues work above the concurrency cap', async () => {
  __attachmentLoaderInternals.resetForTests()

  const releases = []
  for (let i = 0; i < __attachmentLoaderInternals.AUTO_FETCH_LIMIT; i += 1) {
    releases.push(await __attachmentLoaderInternals.acquireAutoFetchSlot())
  }

  let queuedGranted = false
  const queuedSlot = __attachmentLoaderInternals.acquireAutoFetchSlot().then((release) => {
    queuedGranted = true
    return release
  })

  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(queuedGranted, false)

  releases[0]()
  const queuedRelease = await queuedSlot
  assert.equal(queuedGranted, true)

  queuedRelease()
  for (const release of releases.slice(1)) {
    release()
  }
})

test('attachment unavailable cooldowns can be recorded and cleared', () => {
  __attachmentLoaderInternals.resetForTests()

  const entry = __attachmentLoaderInternals.noteBlobFailureCooldown(
    'board-a:blob-a',
    'HTTP 404',
  )

  assert.ok(entry.until > Date.now())
  assert.equal(
    __attachmentLoaderInternals.getBlobFailureCooldown('board-a:blob-a')?.last_error,
    'HTTP 404',
  )

  __attachmentLoaderInternals.clearBlobFailureCooldown('board-a:blob-a')
  assert.equal(
    __attachmentLoaderInternals.getBlobFailureCooldown('board-a:blob-a'),
    null,
  )
})
