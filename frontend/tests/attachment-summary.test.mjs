import test from 'node:test'
import assert from 'node:assert/strict'
import {
  formatAttachmentCountLabel,
  summarizeAttachmentBundle,
  validateAttachmentBundle,
} from '../src/utils/attachmentSummary.js'

test('attachment summary prefers file count for multi-file bundles', () => {
  const summary = summarizeAttachmentBundle({
    declaredCount: 12,
    totalSize: 4096,
  })

  assert.equal(summary.count, 12)
  assert.equal(summary.countLabel, '12 files')
  assert.equal(summary.badgeLabel, '12 files')
  assert.equal(summary.summaryText, '12 files · 4.0 KB')
})

test('attachment summary keeps single-file type label when the MIME type is known', () => {
  const summary = summarizeAttachmentBundle({
    attachments: [{ mime_type: 'image/png', bytes: new Uint8Array([1, 2, 3]) }],
    declaredCount: 1,
    totalSize: 128,
  })

  assert.equal(summary.count, 1)
  assert.equal(summary.typeLabel, 'PNG')
  assert.equal(summary.badgeLabel, 'PNG')
  assert.equal(summary.summaryText, '1 file · 128 B')
})

test('attachment validation rejects mismatched counts but accepts large valid bundles', () => {
  const mismatched = validateAttachmentBundle({
    attachments: [{}, {}],
    declaredCount: 1,
  })
  assert.equal(mismatched.valid, false)
  assert.equal(mismatched.code, 'mismatch')
  assert.match(mismatched.message, /Declared 1 file but decrypted as 2 files\./)

  const validLarge = validateAttachmentBundle({
    attachments: Array.from({ length: 13 }, () => ({})),
    declaredCount: 13,
  })
  assert.equal(validLarge.valid, true)
  assert.equal(validLarge.code, 'ok')
})

test('attachment count labels pluralize cleanly', () => {
  assert.equal(formatAttachmentCountLabel(1), '1 file')
  assert.equal(formatAttachmentCountLabel(7), '7 files')
})
