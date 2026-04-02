const TYPE_LABELS = new Map([
  ['application/gzip', 'GZ'],
  ['application/json', 'JSON'],
  ['application/msword', 'DOC'],
  ['application/octet-stream', 'BIN'],
  ['application/pdf', 'PDF'],
  ['application/vnd.ms-excel', 'XLS'],
  ['application/vnd.ms-powerpoint', 'PPT'],
  ['application/vnd.openxmlformats-officedocument.presentationml.presentation', 'PPTX'],
  ['application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'XLSX'],
  ['application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'DOCX'],
  ['application/x-7z-compressed', '7Z'],
  ['application/x-rar-compressed', 'RAR'],
  ['application/x-tar', 'TAR'],
  ['application/x-zip-compressed', 'ZIP'],
  ['application/zip', 'ZIP'],
  ['audio/flac', 'FLAC'],
  ['audio/mpeg', 'MP3'],
  ['audio/ogg', 'OGG'],
  ['audio/wav', 'WAV'],
  ['image/avif', 'AVIF'],
  ['image/gif', 'GIF'],
  ['image/jpeg', 'JPEG'],
  ['image/png', 'PNG'],
  ['image/svg+xml', 'SVG'],
  ['image/webp', 'WEBP'],
  ['text/csv', 'CSV'],
  ['text/markdown', 'MD'],
  ['text/plain', 'TXT'],
  ['text/x-markdown', 'MD'],
  ['video/mp4', 'MP4'],
  ['video/mpeg', 'MPEG'],
  ['video/quicktime', 'MOV'],
  ['video/webm', 'WEBM'],
  ['video/x-matroska', 'MKV'],
  ['video/x-msvideo', 'AVI'],
])

function normalizeMimeType(mimeType) {
  const normalized = String(mimeType || '').trim().toLowerCase()
  if (!normalized) return 'application/octet-stream'
  return normalized.split(';', 1)[0]
}

function fallbackTypeLabel(mimeType) {
  const normalized = normalizeMimeType(mimeType)
  const parts = normalized.split('/')
  const subtype = parts[1] || ''
  if (!subtype) return 'FILE'

  const simplified = subtype
    .replace(/^x-/, '')
    .replace(/^vnd\./, '')
    .split('+')[0]
    .split('.')
    .pop()
    .replace(/[^a-z0-9]/g, '')

  if (!simplified) {
    if (parts[0] === 'text') return 'TXT'
    if (parts[0] === 'audio') return 'AUDIO'
    if (parts[0] === 'video') return 'VIDEO'
    if (parts[0] === 'image') return 'IMAGE'
    return 'FILE'
  }

  return simplified.toUpperCase().slice(0, 8)
}

export function getAttachmentTypeLabel(mimeType) {
  const normalized = normalizeMimeType(mimeType)
  return TYPE_LABELS.get(normalized) || fallbackTypeLabel(normalized)
}

export function normalizeAttachmentFilename(filename) {
  if (typeof filename !== 'string') return ''
  return filename
    .replace(/[\\\/]/g, '_')
    .replace(/[\u0000-\u001f\u007f]/g, '')
    .replace(/_+/g, '_')
    .trim()
}

export function getAttachmentDownloadName(attachment) {
  const filename = normalizeAttachmentFilename(attachment?.filename)
  if (filename) return filename

  const normalized = normalizeMimeType(attachment?.mime_type)
  const subtype = normalized.split('/')[1] || ''
  const ext = subtype.split('+')[0] || 'bin'
  return `file.${ext}`
}

export function isImageType(mimeType) {
  return normalizeMimeType(mimeType).startsWith('image/')
}

export function isVideoType(mimeType) {
  return normalizeMimeType(mimeType).startsWith('video/')
}

export function isAudioType(mimeType) {
  return normalizeMimeType(mimeType).startsWith('audio/')
}

export function isPreviewableType(mimeType) {
  return isImageType(mimeType) || isVideoType(mimeType)
}

export function getAttachmentCategory(mimeType) {
  const normalized = normalizeMimeType(mimeType)
  if (normalized.startsWith('image/')) return 'image'
  if (normalized.startsWith('video/')) return 'video'
  if (normalized.startsWith('audio/')) return 'audio'
  if (normalized.startsWith('text/')) return 'text'
  if (
    normalized === 'application/zip' ||
    normalized === 'application/x-zip-compressed' ||
    normalized === 'application/x-rar-compressed' ||
    normalized === 'application/x-7z-compressed' ||
    normalized === 'application/gzip' ||
    normalized === 'application/x-tar'
  ) {
    return 'archive'
  }
  if (
    normalized === 'application/msword' ||
    normalized === 'application/vnd.ms-excel' ||
    normalized === 'application/vnd.ms-powerpoint' ||
    normalized === 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' ||
    normalized === 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' ||
    normalized === 'application/vnd.openxmlformats-officedocument.presentationml.presentation' ||
    normalized === 'application/pdf'
  ) {
    return 'document'
  }
  return 'file'
}

export function describeAttachment(mimeType) {
  const normalized = normalizeMimeType(mimeType)
  return {
    mimeType: normalized,
    typeLabel: getAttachmentTypeLabel(normalized),
    category: getAttachmentCategory(normalized),
    isPreviewable: isPreviewableType(normalized),
    isImage: isImageType(normalized),
    isVideo: isVideoType(normalized),
    isAudio: isAudioType(normalized),
  }
}
