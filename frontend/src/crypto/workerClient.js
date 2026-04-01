/**
 * Client for the RetiBoard Crypto Worker.
 *
 * Provides a promise-based RPC interface to delegate heavy tasks.
 * Falls back to main-thread execution if Web Workers are unavailable
 * or blocked by browser security extensions (e.g., JShelter).
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

let worker = null
let workerFailed = false
const pendingPromises = new Map()
let nextTaskId = 1

function initWorker() {
  if (worker) return worker
  if (workerFailed || typeof Worker === 'undefined') return null

  try {
    // Vite handles the Worker URL + type: module automatically.
    worker = new Worker(new URL('./worker.js', import.meta.url), { type: 'module' })

    worker.onmessage = (event) => {
      const { id, result, error, type, attempts } = event.data

      if (type === 'powProgress') {
        const p = pendingPromises.get(id)
        if (p?.onProgress) {
          p.onProgress(attempts)
        }
        return
      }

      const p = pendingPromises.get(id)
      if (p) {
        pendingPromises.delete(id)
        if (error) {
          p.reject(new Error(error))
        } else {
          p.resolve(result)
        }
      }
    }

    worker.onerror = (err) => {
      console.warn('Crypto Worker failed to start, falling back to main thread.', err)
      workerFailed = true
      worker = null
      // Reject all pending worker tasks so they can be retried or handled.
      for (const [id, p] of pendingPromises) {
        p.reject(new Error('Worker failed'))
        pendingPromises.delete(id)
      }
    }

    return worker
  } catch (e) {
    console.warn('Web Workers are blocked or unsupported. Falling back to main thread.', e)
    workerFailed = true
    return null
  }
}

/**
 * Run a task in the Web Worker with main-thread fallback.
 */
export async function runTask(type, payload, transfer = [], onProgress = null) {
  const w = initWorker()

  if (w && !workerFailed) {
    const id = nextTaskId++
    return new Promise((resolve, reject) => {
      pendingPromises.set(id, { resolve, reject, onProgress })
      try {
        w.postMessage({ id, type, payload }, transfer)
      } catch (e) {
        // Handle serialization errors or sudden worker death.
        pendingPromises.delete(id)
        console.warn('Worker postMessage failed, falling back.', e)
        resolve(runTaskFallback(type, payload, onProgress))
      }
    })
  }

  return runTaskFallback(type, payload, onProgress)
}

/**
 * Synchronous-looking fallback using logic.js on the main thread.
 */
async function runTaskFallback(type, payload, onProgress) {
  switch (type) {
    case 'encryptPayload':
      return encryptPayloadLogic(payload.boardKey, payload.text, payload.attachments)
    case 'decryptTextPayload':
      return decryptTextPayloadLogic(payload.boardKey, payload.blob)
    case 'decryptAttachmentPayloadLowMem':
      return decryptAttachmentPayloadLowMemLogic(payload.boardKey, payload.blob)
    case 'decryptPayload':
      return decryptPayloadLogic(payload.boardKey, payload.blob)
    case 'computeSHA256Hex':
      return computeSHA256HexLogic(payload.blob)
    case 'verifyContentHash':
      return verifyContentHashLogic(payload.blob, payload.expectedHash)
    case 'solvePoW':
      return solvePoWLogic(payload.metadata, payload.difficulty, onProgress)
    default:
      throw new Error(`Unknown task type: ${type}`)
  }
}
