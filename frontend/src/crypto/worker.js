/**
 * RetiBoard Crypto Worker
 *
 * Offloads SHA-256, AES-GCM, MsgPack, and Blob creation from the main thread.
 * Thin wrapper around logic.js.
 */

import {
  encryptPayloadLogic,
  decryptTextPayloadLogic,
  decryptAttachmentPayloadLowMemLogic,
  decryptPayloadLogic,
  solvePoWLogic,
  computeSHA256HexLogic,
  verifyContentHashLogic
} from './logic.js'

self.onmessage = async (event) => {
  const { id, type, payload } = event.data

  try {
    let result
    let transfer = []

    switch (type) {
      case 'encryptPayload':
        result = await encryptPayloadLogic(payload.boardKey, payload.text, payload.attachments)
        if (result.textBlob) transfer.push(result.textBlob.buffer)
        if (result.attachmentBlob) transfer.push(result.attachmentBlob.buffer)
        break

      case 'decryptTextPayload':
        result = await decryptTextPayloadLogic(payload.boardKey, payload.blob)
        break

      case 'decryptAttachmentPayloadLowMem':
        result = await decryptAttachmentPayloadLowMemLogic(payload.boardKey, payload.blob)
        break

      case 'decryptPayload':
        result = await decryptPayloadLogic(payload.boardKey, payload.blob)
        if (result.attachments) {
           for (const att of result.attachments) {
             if (att.bytes) transfer.push(att.bytes.buffer)
           }
        }
        break

      case 'computeSHA256Hex':
        result = await computeSHA256HexLogic(payload.blob)
        break

      case 'verifyContentHash':
        result = await verifyContentHashLogic(payload.blob, payload.expectedHash)
        break

      case 'solvePoW':
        result = await solvePoWLogic(payload.metadata, payload.difficulty, (attempts) => {
          self.postMessage({ id, type: 'powProgress', attempts })
        })
        break

      default:
        throw new Error(`Unknown task type: ${type}`)
    }

    self.postMessage({ id, result }, transfer)
  } catch (error) {
    self.postMessage({ id, error: error.message })
  }
}
