import { describeAttachment } from './attachments.js'

export const INLINE_ATTACHMENT_RENDER_LIMIT = 4

export function formatAttachmentBytes(bytes) {
  const size = Number(bytes || 0)
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / (1024 * 1024)).toFixed(1)} MB`
}

export function formatAttachmentCountLabel(count) {
  const normalized = Math.max(0, Number(count || 0))
  return `${normalized} file${normalized === 1 ? '' : 's'}`
}

export function summarizeAttachmentBundle(options = {}) {
  const attachments = Array.isArray(options.attachments) ? options.attachments : null
  const actualCount = attachments?.length || 0
  const declaredCount = Math.max(0, Number(options.declaredCount || 0))
  const count = actualCount || declaredCount
  const primaryMimeType = attachments?.[0]?.mime_type || options.primaryMimeType || ''
  const primary = describeAttachment(primaryMimeType)
  const totalSize = Math.max(0, Number(options.totalSize || 0))
  const countLabel = formatAttachmentCountLabel(count)

  return {
    ...primary,
    count,
    declaredCount,
    actualCount,
    totalSize,
    countLabel,
    summaryText: `${countLabel} · ${formatAttachmentBytes(totalSize)}`,
    badgeLabel: count > 1 ? countLabel : primary.typeLabel,
    isMulti: count > 1,
  }
}

export function validateAttachmentBundle(options = {}) {
  const attachments = Array.isArray(options.attachments) ? options.attachments : []
  const actualCount = attachments.length
  const declaredCount = Math.max(0, Number(options.declaredCount || 0))

  if (actualCount <= 0) {
    return {
      valid: false,
      code: 'empty',
      message: 'Attachment bundle was empty after decryption.',
    }
  }

  const problems = []
  let code = 'ok'

  if (declaredCount > 0 && actualCount !== declaredCount) {
    code = 'mismatch'
    problems.push(
      `Declared ${formatAttachmentCountLabel(declaredCount)} but decrypted as ${formatAttachmentCountLabel(actualCount)}.`,
    )
  }

  if (problems.length) {
    return {
      valid: false,
      code,
      message: problems.join(' '),
    }
  }

  return {
    valid: true,
    code: 'ok',
    message: '',
  }
}
