/**
 * Core RetiBoard Crypto Logic
 *
 * This file contains the actual implementation of all heavy tasks.
 * It is designed to be used both inside a Web Worker and on the main thread
 * as a fallback for hardened browsers.
 */

import { decode as msgpackDecode, encode as msgpackEncode } from '@msgpack/msgpack'

const NONCE_LENGTH = 12
const MIN_BLOB_SIZE = NONCE_LENGTH + 16 + 1

/**
 * Encrypt text and attachments into separate blobs.
 */
export async function encryptPayloadLogic(boardKey, text, attachments = []) {
  const textResult = await _encryptBlob(boardKey, msgpackEncode({ text }))

  const hasAttachments = attachments.length > 0
  let attachmentBlob = null
  let attachmentContentHash = ''
  let attachmentPayloadSize = 0

  if (hasAttachments) {
    const attachmentPacked = msgpackEncode({
      attachments: attachments.map(att => ({
        filename: typeof att.filename === 'string' ? att.filename : '',
        mime_type: att.mime_type,
        data: att.bytes instanceof Uint8Array ? att.bytes : new Uint8Array(att.bytes),
      }))
    })
    const attachmentResult = await _encryptBlob(boardKey, attachmentPacked)
    attachmentBlob = attachmentResult.blob
    attachmentContentHash = attachmentResult.contentHash
    attachmentPayloadSize = attachmentResult.blob.length
  }

  return {
    textBlob: textResult.blob,
    contentHash: textResult.contentHash,
    payloadSize: textResult.blob.length,
    attachmentBlob,
    attachmentContentHash,
    attachmentPayloadSize,
    hasAttachments,
  }
}

/**
 * Decrypt a text-only blob.
 */
export async function decryptTextPayloadLogic(boardKey, blob) {
  if (!blob || blob.length < MIN_BLOB_SIZE) {
    return { text: null, error: 'Payload too small or missing' }
  }
  try {
    const plaintext = await _decryptBlob(boardKey, blob)
    const payload = msgpackDecode(new Uint8Array(plaintext))
    if (typeof payload.text !== 'string') {
      return { text: null, error: 'Malformed text payload' }
    }
    return { text: payload.text, error: null }
  } catch (err) {
    return { text: null, error: `Decryption failed: ${err.message}` }
  }
}

/**
 * Decrypt attachments and convert to Blobs.
 */
export async function decryptAttachmentPayloadLowMemLogic(boardKey, encryptedBlob) {
  if (!encryptedBlob || encryptedBlob.length < MIN_BLOB_SIZE) {
    return { attachments: null, error: 'Attachment payload too small or missing' }
  }

  let plaintext
  try {
    const nonce = encryptedBlob.subarray(0, NONCE_LENGTH)
    const ciphertext = encryptedBlob.subarray(NONCE_LENGTH)
    plaintext = await crypto.subtle.decrypt(
      { name: 'AES-GCM', iv: nonce },
      boardKey,
      ciphertext,
    )

    // Help GC: encryptedBlob is no longer needed after decryption.
    encryptedBlob = null

    const payload = msgpackDecode(new Uint8Array(plaintext))
    if (!Array.isArray(payload.attachments) || payload.attachments.length === 0) {
      return { attachments: null, error: 'No attachments in attachment payload' }
    }

    const attachments = []
    for (const att of payload.attachments) {
      const rawBytes = att?.data instanceof Uint8Array
        ? att.data
        : new Uint8Array(att?.data || [])
      const mimeType = att?.mime_type || 'application/octet-stream'
      
      // Hash check (still requires whole rawBytes in memory for digest).
      const fileHash = await computeSHA256HexLogic(rawBytes)
      
      // Creating a Blob often allows the browser to move data out of JS heap.
      const blob = new Blob([rawBytes], { type: mimeType })
      
      attachments.push({
        filename: typeof att?.filename === 'string' ? att.filename : '',
        mime_type: mimeType,
        blob,
        size: blob.size,
        file_hash: fileHash,
      })

      // Help GC: remove reference to the Uint8Array slice of plaintext.
      if (att) att.data = null
    }

    // Help GC: once all Blobs are created, plaintext ArrayBuffer should be eligible for GC
    // if the Blobs aren't keeping a direct reference to it (browser dependent, but better than nothing).
    plaintext = null

    return { attachments, error: null }
  } catch (err) {
    return { attachments: null, error: `Attachment decryption failed: ${err.message}` }
  }
}

/**
 * Decrypt combined payload (legacy or general).
 */
export async function decryptPayloadLogic(boardKey, blob) {
  if (!blob || blob.length < MIN_BLOB_SIZE) {
    return { text: null, attachments: null, error: 'Payload too small or missing' }
  }
  try {
    const plaintext = await _decryptBlob(boardKey, blob)
    const payload = msgpackDecode(new Uint8Array(plaintext))

    if (typeof payload.text !== 'string') {
      return { text: null, attachments: null, error: 'Malformed payload' }
    }

    let attachments = null
    if (Array.isArray(payload.attachments) && payload.attachments.length > 0) {
      attachments = []
      for (const att of payload.attachments) {
        const bytes = att?.data instanceof Uint8Array ? att.data : new Uint8Array(att?.data || [])
        attachments.push({
          filename: typeof att?.filename === 'string' ? att.filename : '',
          mime_type: att?.mime_type || 'application/octet-stream',
          bytes,
          size: bytes.byteLength,
          file_hash: await computeSHA256HexLogic(bytes),
        })
      }
    }

    return { text: payload.text, attachments, error: null }
  } catch (err) {
    return { text: null, attachments: null, error: `Decryption failed: ${err.message}` }
  }
}

/**
 * Proof of Work solver.
 */
export async function solvePoWLogic(meta, difficulty, onProgress = null) {
  const powFields = [
    'bump_flag', 'content_hash', 'has_attachments',
    'attachment_content_hash', 'attachment_count', 'attachment_payload_size',
    'identity_hash', 'parent_id', 'payload_size', 'post_id',
    'text_only', 'thread_id', 'timestamp', 'public_key',
    'encrypted_pings', 'edit_signature',
  ]
  const sorted = {}
  for (const k of powFields.sort()) {
    if (!(k in meta)) continue
    if (k === 'encrypted_pings' && Array.isArray(meta[k])) {
      sorted[k] = [...meta[k]].filter((item) => typeof item === 'string').sort()
      continue
    }
    sorted[k] = meta[k]
  }
  const canonicalJson = JSON.stringify(sorted)

  const target = BigInt(2) ** BigInt(256 - difficulty)
  const encoder = new TextEncoder()

  for (let i = 0; i < 10_000_000; i++) {
    const nonce = _generateId()
    const preimage = encoder.encode(canonicalJson + nonce)
    const hashBuf = await crypto.subtle.digest('SHA-256', preimage)
    const hashHex = Array.from(new Uint8Array(hashBuf))
      .map(b => b.toString(16).padStart(2, '0')).join('')
    const hashInt = BigInt('0x' + hashHex)
    if (hashInt < target) return nonce
    if (i % 1000 === 0 && onProgress) {
       onProgress(i)
    }
  }
  throw new Error('PoW failed: max iterations exceeded')
}

export async function computeSHA256HexLogic(blob) {
  const hash = await crypto.subtle.digest('SHA-256', blob)
  return _bytesToHex(new Uint8Array(hash))
}

export async function verifyContentHashLogic(blob, expectedHash) {
  const hash = await computeSHA256HexLogic(blob)
  return hash === expectedHash
}

// Internal helpers

async function _encryptBlob(boardKey, plaintext) {
  const nonce = crypto.getRandomValues(new Uint8Array(12))
  const ciphertext = await crypto.subtle.encrypt(
    { name: 'AES-GCM', iv: nonce }, boardKey, plaintext
  )
  // Help GC: plaintext is no longer needed.
  plaintext = null

  const ct = new Uint8Array(ciphertext)
  const blob = new Uint8Array(12 + ct.length)
  blob.set(nonce, 0)
  blob.set(ct, 12)
  const hash = await crypto.subtle.digest('SHA-256', blob)
  return { blob, contentHash: _bytesToHex(new Uint8Array(hash)) }
}

async function _decryptBlob(boardKey, blob) {
  const nonce = blob.subarray(0, NONCE_LENGTH)
  const ciphertext = blob.subarray(NONCE_LENGTH)
  return crypto.subtle.decrypt({ name: 'AES-GCM', iv: nonce }, boardKey, ciphertext)
}

function _bytesToHex(bytes) {
  return Array.from(bytes).map(b => b.toString(16).padStart(2, '0')).join('')
}

function _generateId() {
  const bytes = new Uint8Array(16)
  crypto.getRandomValues(bytes)
  return Array.from(bytes).map(b => b.toString(16).padStart(2, '0')).join('')
}
