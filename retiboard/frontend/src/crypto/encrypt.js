/**
 * AES-GCM payload encryption — split-blob model.
 *
 * Encryption and hashing are offloaded to a Web Worker.
 */

import { runTask } from './workerClient.js'

/**
 * Encrypt a post's text and attachments into separate blobs (in worker).
 *
 * @param {CryptoKey} boardKey
 * @param {string}    text
 * @param {Array}     attachments - [{ filename?, mime_type, bytes: Uint8Array }]
 * @returns {Promise<{
 *   textBlob, contentHash, payloadSize,
 *   attachmentBlob, attachmentContentHash, attachmentPayloadSize,
 *   hasAttachments
 * }>}
 */
export async function encryptPayload(boardKey, text, attachments = []) {
  const transfer = []
  for (const att of attachments) {
    if (att.bytes instanceof Uint8Array) {
      transfer.push(att.bytes.buffer)
    }
  }
  return runTask('encryptPayload', { boardKey, text, attachments }, transfer)
}
