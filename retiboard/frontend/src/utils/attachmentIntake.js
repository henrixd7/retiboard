import { normalizeAttachmentFilename } from './attachments.js'

function normalizeMimeType(mimeType) {
  const normalized = String(mimeType || '').trim().toLowerCase()
  if (!normalized) return 'application/octet-stream'
  return normalized.split(';', 1)[0]
}

export function collectFilesFromDataTransfer(dataTransfer) {
  if (!dataTransfer) return []

  const itemFiles = []
  if (Array.isArray(dataTransfer.items) || dataTransfer.items?.length) {
    for (const item of Array.from(dataTransfer.items || [])) {
      if (item?.kind !== 'file' || typeof item.getAsFile !== 'function') continue
      const file = item.getAsFile()
      if (file) itemFiles.push(file)
    }
  }
  if (itemFiles.length > 0) return itemFiles

  return Array.from(dataTransfer.files || []).filter(Boolean)
}

export function hasFileDataTransfer(dataTransfer) {
  if (!dataTransfer) return false
  if (dataTransfer.files?.length > 0) return true
  return Array.from(dataTransfer.types || []).includes('Files')
}

export function getDraftAttachmentName(file) {
  const explicitName = normalizeAttachmentFilename(file?.name || '')
  if (explicitName) return explicitName

  const normalizedMime = normalizeMimeType(file?.type)
  const subtype = normalizedMime.split('/')[1] || ''
  const ext = subtype.split('+')[0] || 'bin'
  return `pasted.${ext}`
}
