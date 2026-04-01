/**
 * Board key derivation using HKDF-SHA-256 via Web Crypto API.
 *
 * Spec references:
 *   §5  — "Board-wide AES-GCM key derived from key_material via HKDF (frontend only)."
 *   §10 — "Board key held in memory only (re-derived per session)."
 *   §17 — "No browser persistent storage for keys or metadata."
 *
 * Design invariants:
 *   - key_material comes from the API (GET /api/boards/{id}).
 *   - The derived AES-GCM CryptoKey NEVER leaves this module.
 *   - NEVER store the key in localStorage, IndexedDB, cookies, etc.
 *   - On page reload, the key must be re-derived from key_material.
 *   - The backend NEVER sees the derived key.
 *
 * Key derivation:
 *   1. Import key_material (hex string) as raw HKDF key material
 *   2. Derive a 256-bit AES-GCM key using HKDF-SHA-256
 *      - salt: "retiboard-v1" (fixed, protocol-level)
 *      - info: board_id (binds the key to a specific board)
 */

// Fixed salt for HKDF derivation — protocol-level constant.
// Changing this breaks all existing board keys.
const HKDF_SALT = new TextEncoder().encode('retiboard-v1')

// In-memory key cache: { boardId: CryptoKey }
// Cleared on page unload — keys never persist.
const keyCache = new Map()

/**
 * Derive the AES-GCM board key from key_material.
 *
 * @param {string} keyMaterial - Hex string from the board announce (§3.3)
 * @param {string} boardId    - Board identifier (used as HKDF info)
 * @returns {Promise<CryptoKey>} AES-GCM key for encrypt/decrypt
 */
export async function deriveBoardKey(keyMaterial, boardId) {
  // Check cache first (avoid redundant derivation within a session).
  const cacheKey = `${boardId}:${keyMaterial}`
  if (keyCache.has(cacheKey)) {
    return keyCache.get(cacheKey)
  }

  // 1. Decode hex key_material to bytes.
  const rawBytes = hexToBytes(keyMaterial)

  // 2. Import as HKDF key material (not directly usable for encryption).
  const hkdfKey = await crypto.subtle.importKey(
    'raw',
    rawBytes,
    { name: 'HKDF' },
    false,           // not extractable
    ['deriveKey']
  )

  // 3. Derive AES-GCM-256 key using HKDF-SHA-256.
  const info = new TextEncoder().encode(boardId)
  const aesKey = await crypto.subtle.deriveKey(
    {
      name: 'HKDF',
      hash: 'SHA-256',
      salt: HKDF_SALT,
      info: info,
    },
    hkdfKey,
    { name: 'AES-GCM', length: 256 },
    false,           // not extractable — key stays in CryptoKey object
    ['encrypt', 'decrypt']
  )

  // Cache in memory (cleared on page unload).
  keyCache.set(cacheKey, aesKey)

  return aesKey
}

/**
 * Clear all cached keys. Call on logout or board switch.
 */
export function clearKeyCache() {
  keyCache.clear()
}

/**
 * Convert a hex string to a Uint8Array.
 * @param {string} hex
 * @returns {Uint8Array}
 */
function hexToBytes(hex) {
  if (hex.length % 2 !== 0) {
    throw new Error('Invalid hex string: odd length')
  }
  const bytes = new Uint8Array(hex.length / 2)
  for (let i = 0; i < hex.length; i += 2) {
    bytes[i / 2] = parseInt(hex.substring(i, i + 2), 16)
  }
  return bytes
}
