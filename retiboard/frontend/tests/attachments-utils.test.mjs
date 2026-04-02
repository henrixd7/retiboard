import test from 'node:test'
import assert from 'node:assert/strict'
import {
  describeAttachment,
  getAttachmentDownloadName,
  getAttachmentTypeLabel,
  isPreviewableType,
  normalizeAttachmentFilename,
} from '../src/utils/attachments.js'

test('attachment labels prefer real file-style types for common MIME values', () => {
  assert.equal(getAttachmentTypeLabel('video/x-msvideo'), 'AVI')
  assert.equal(getAttachmentTypeLabel('video/quicktime'), 'MOV')
  assert.equal(getAttachmentTypeLabel('video/mp4'), 'MP4')
  assert.equal(getAttachmentTypeLabel('image/jpeg'), 'JPEG')
  assert.equal(getAttachmentTypeLabel('text/markdown'), 'MD')
  assert.equal(
    getAttachmentTypeLabel('application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
    'DOCX',
  )
  assert.equal(getAttachmentTypeLabel('application/zip'), 'ZIP')
})

test('attachment descriptions distinguish previewable files from generic files', () => {
  assert.equal(isPreviewableType('image/avif'), true)
  assert.equal(isPreviewableType('video/webm'), true)
  assert.equal(isPreviewableType('application/pdf'), false)

  assert.deepEqual(describeAttachment('application/pdf'), {
    mimeType: 'application/pdf',
    typeLabel: 'PDF',
    category: 'document',
    isPreviewable: false,
    isImage: false,
    isVideo: false,
    isAudio: false,
  })
})

test('attachment filenames are normalized and preferred for downloads', () => {
  assert.equal(normalizeAttachmentFilename('  hello.png  '), 'hello.png')
  assert.equal(normalizeAttachmentFilename('nested/path\\\\name.png'), 'nested_path_name.png')
  assert.equal(normalizeAttachmentFilename('bad\u0000name.txt'), 'badname.txt')

  assert.equal(
    getAttachmentDownloadName({ filename: ' photo final.webp ', mime_type: 'image/webp' }),
    'photo final.webp',
  )
  assert.equal(
    getAttachmentDownloadName({ filename: '', mime_type: 'application/pdf' }),
    'file.pdf',
  )
  assert.equal(
    getAttachmentDownloadName({ mime_type: 'application/octet-stream' }),
    'file.octet-stream',
  )
})
