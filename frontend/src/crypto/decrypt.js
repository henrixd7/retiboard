/**
 * AES-GCM payload decryption — split-blob model.
 *
 * Heavy lifting (decryption, hashing, msgpack decoding) is offloaded
 * to a dedicated Web Worker to keep the main thread responsive.
 */

import { runTask } from './workerClient.js'

/**
 * Decrypt a text-only blob → { text, error }
 */
export async function decryptTextPayload(boardKey, blob) {
  const transfer = blob instanceof Uint8Array ? [blob.buffer] : []
  return runTask('decryptTextPayload', { boardKey, blob }, transfer)
}

/**
 * Decrypt an attachment-only blob → { attachments, error }
 */
export async function decryptAttachmentPayload(boardKey, blob) {
  // Uses the legacy path in worker.
  const transfer = blob instanceof Uint8Array ? [blob.buffer] : []
  return runTask('decryptPayload', { boardKey, blob }, transfer)
}

/**
 * Lower-memory attachment decrypt path.
 *
 * Offloads decryption, msgpack decoding, and Blob creation to the worker.
 */
export async function decryptAttachmentPayloadLowMem(boardKey, blob) {
  const transfer = blob instanceof Uint8Array ? [blob.buffer] : []
  return runTask('decryptAttachmentPayloadLowMem', { boardKey, blob }, transfer)
}

/**
 * Legacy: decrypt a combined blob → { text, attachments, error }
 * For backward compat with posts created before split-blob.
 */
export async function decryptPayload(boardKey, blob) {
  const transfer = blob instanceof Uint8Array ? [blob.buffer] : []
  return runTask('decryptPayload', { boardKey, blob }, transfer)
}

/**
 * Fetch a payload, enforce an optional size ceiling, verify its SHA-256,
 * and return the raw bytes.
 */
export async function fetchAndVerify(url, expectedHash, options = {}) {
  // Inject ephemeral API token if present in sessionStorage (§15).
  const token = sessionStorage.getItem('retiboard_token')
  const headers = new Headers(options.headers || {})
  if (token) {
    headers.set('X-RetiBoard-Token', token)
  }

  const res = await fetch(url, { 
    headers,
    signal: options.signal, 
    cache: 'no-store' 
  })
  if (!res.ok) {
    return { data: null, error: `HTTP ${res.status}` }
  }

  const declaredSize = Number(res.headers.get('content-length') || 0)
  const maxBytes = Number(options.maxBytes || 0)
  if (maxBytes > 0 && declaredSize > 0 && declaredSize > maxBytes) {
    try {
      await res.body?.cancel?.()
    } catch {
      // Ignore cancellation failures on oversized responses.
    }
    return { data: null, error: `Payload too large: ${declaredSize} bytes` }
  }

  const data = new Uint8Array(await res.arrayBuffer())
  
  // Verify hash in the worker.
  if (!(await verifyContentHash(data, expectedHash))) {
    return { data: null, error: 'Hash mismatch' }
  }
  return { data, error: null, contentLength: declaredSize }
}

/**
 * Verify SHA-256 hash of a blob (in worker).
 */
export async function verifyContentHash(blob, expectedHash) {
  return runTask('verifyContentHash', { blob, expectedHash })
}

/**
 * Compute SHA-256 hex string of a blob (in worker).
 */
export async function computeSHA256Hex(blob) {
  return runTask('computeSHA256Hex', { blob })
}
