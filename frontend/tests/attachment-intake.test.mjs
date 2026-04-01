import test from 'node:test'
import assert from 'node:assert/strict'
import {
  collectFilesFromDataTransfer,
  getDraftAttachmentName,
  hasFileDataTransfer,
} from '../src/utils/attachmentIntake.js'

test('collectFilesFromDataTransfer prefers item files and filters nulls', () => {
  const first = { name: 'one.png', type: 'image/png' }
  const second = { name: 'two.txt', type: 'text/plain' }

  const files = collectFilesFromDataTransfer({
    items: [
      { kind: 'string', getAsFile: () => null },
      { kind: 'file', getAsFile: () => first },
      { kind: 'file', getAsFile: () => null },
      { kind: 'file', getAsFile: () => second },
    ],
    files: [{ name: 'fallback.bin' }],
  })

  assert.deepEqual(files, [first, second])
})

test('collectFilesFromDataTransfer falls back to dataTransfer.files', () => {
  const fallback = [{ name: 'drop.webp' }, { name: 'drop-2.webp' }]
  const files = collectFilesFromDataTransfer({ files: fallback })
  assert.deepEqual(files, fallback)
})

test('hasFileDataTransfer detects file drags without consuming payload', () => {
  assert.equal(hasFileDataTransfer({ files: [{ name: 'x' }] }), true)
  assert.equal(hasFileDataTransfer({ types: ['text/plain', 'Files'] }), true)
  assert.equal(hasFileDataTransfer({ types: ['text/plain'] }), false)
})

test('getDraftAttachmentName preserves explicit names and synthesizes clipboard names', () => {
  assert.equal(getDraftAttachmentName({ name: '  photo final.png  ', type: 'image/png' }), 'photo final.png')
  assert.equal(getDraftAttachmentName({ name: '', type: 'image/png' }), 'pasted.png')
  assert.equal(getDraftAttachmentName({ type: 'application/octet-stream' }), 'pasted.octet-stream')
})
