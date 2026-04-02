function attachmentSize(att) {
  return Number(att?.size || att?.blob?.size || att?.bytes?.byteLength || att?.bytes?.length || 0)
}

export function isLocalBannedFilePlaceholder(attachment) {
  return attachment?.placeholder_kind === 'banned_local'
}

export function buildBannedFilePlaceholder(attachment) {
  return {
    filename: typeof attachment?.filename === 'string' ? attachment.filename : '',
    mime_type: attachment?.mime_type || 'application/octet-stream',
    file_hash: String(attachment?.file_hash || '').trim(),
    size: attachmentSize(attachment),
    placeholder_kind: 'banned_local',
  }
}

export function sanitizeAttachmentListForLocalBans(attachments, isFileHashBanned) {
  if (!Array.isArray(attachments) || attachments.length === 0) {
    return { attachments: [], changed: false, hasBannedFiles: false }
  }

  let changed = false
  const sanitized = attachments.map((attachment) => {
    if (isLocalBannedFilePlaceholder(attachment)) {
      return attachment
    }

    const hash = String(attachment?.file_hash || '').trim()
    if (!hash || !isFileHashBanned?.(hash)) {
      return attachment
    }

    changed = true
    return buildBannedFilePlaceholder(attachment)
  })

  return {
    attachments: sanitized,
    changed,
    hasBannedFiles: sanitized.some(isLocalBannedFilePlaceholder),
  }
}
