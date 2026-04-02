import test from 'node:test'
import assert from 'node:assert/strict'
import {
  buildBannedFilePlaceholder,
  isLocalBannedFilePlaceholder,
  sanitizeAttachmentListForLocalBans,
} from '../src/utils/attachmentModeration.js'

test('sanitizeAttachmentListForLocalBans replaces banned files with structural placeholders', () => {
  const attachments = [
    { filename: 'safe.png', mime_type: 'image/png', file_hash: 'safe', size: 12, blob: { size: 12 } },
    { filename: 'bad.png', mime_type: 'image/png', file_hash: 'banned', size: 34, blob: { size: 34 } },
  ]

  const result = sanitizeAttachmentListForLocalBans(attachments, (hash) => hash === 'banned')

  assert.equal(result.changed, true)
  assert.equal(result.hasBannedFiles, true)
  assert.equal(result.attachments.length, 2)
  assert.equal(result.attachments[0], attachments[0])
  assert.equal(isLocalBannedFilePlaceholder(result.attachments[1]), true)
  assert.equal(result.attachments[1].file_hash, 'banned')
  assert.equal(result.attachments[1].size, 34)
})

test('buildBannedFilePlaceholder keeps structural file identity but drops content payloads', () => {
  const placeholder = buildBannedFilePlaceholder({
    filename: 'clip.webm',
    mime_type: 'video/webm',
    file_hash: 'abc123',
    size: 99,
    blob: { size: 99 },
  })

  assert.deepEqual(placeholder, {
    filename: 'clip.webm',
    mime_type: 'video/webm',
    file_hash: 'abc123',
    size: 99,
    placeholder_kind: 'banned_local',
  })
})
