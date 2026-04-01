const CURVE = 'P-256'
const AES_KEY_LENGTH = 256
const IV_LENGTH = 12

function bytesToHex(bytes) {
  return Array.from(bytes)
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('')
}

function hexToBytes(hex) {
  if (typeof hex !== 'string' || hex.length % 2 !== 0) {
    throw new Error('Invalid hex input')
  }

  const bytes = new Uint8Array(hex.length / 2)
  for (let i = 0; i < hex.length; i += 2) {
    const value = parseInt(hex.slice(i, i + 2), 16)
    if (Number.isNaN(value)) {
      throw new Error('Invalid hex input')
    }
    bytes[i / 2] = value
  }
  return bytes
}

async function importPublicKey(publicKeyHex) {
  return window.crypto.subtle.importKey(
    'raw',
    hexToBytes(publicKeyHex),
    { name: 'ECDH', namedCurve: CURVE },
    false,
    [],
  )
}

export async function generateEphemeralKey() {
  const keyPair = await window.crypto.subtle.generateKey(
    { name: 'ECDH', namedCurve: CURVE },
    true,
    ['deriveKey'],
  )

  const publicKeyBytes = await window.crypto.subtle.exportKey('raw', keyPair.publicKey)
  return {
    publicKeyHex: bytesToHex(new Uint8Array(publicKeyBytes)),
    privateKeyObj: keyPair.privateKey,
  }
}

export async function encryptPing(targetPublicKeyHex, pingDataString) {
  const targetPublicKey = await importPublicKey(targetPublicKeyHex)
  const tempKeyPair = await window.crypto.subtle.generateKey(
    { name: 'ECDH', namedCurve: CURVE },
    true,
    ['deriveKey'],
  )
  const aesKey = await window.crypto.subtle.deriveKey(
    { name: 'ECDH', public: targetPublicKey },
    tempKeyPair.privateKey,
    { name: 'AES-GCM', length: AES_KEY_LENGTH },
    false,
    ['encrypt'],
  )

  const iv = new Uint8Array(IV_LENGTH)
  window.crypto.getRandomValues(iv)

  const ciphertext = await window.crypto.subtle.encrypt(
    { name: 'AES-GCM', iv },
    aesKey,
    new TextEncoder().encode(String(pingDataString)),
  )
  const tempPublicKeyBytes = await window.crypto.subtle.exportKey('raw', tempKeyPair.publicKey)

  return [
    bytesToHex(new Uint8Array(tempPublicKeyBytes)),
    bytesToHex(iv),
    bytesToHex(new Uint8Array(ciphertext)),
  ].join(':')
}

export async function decryptPing(myPrivateKeyObj, encryptedPingString) {
  try {
    const [tempPubKeyHex, ivHex, ciphertextHex] = String(encryptedPingString).split(':')
    if (!tempPubKeyHex || !ivHex || !ciphertextHex) return null

    const tempPublicKey = await importPublicKey(tempPubKeyHex)
    const aesKey = await window.crypto.subtle.deriveKey(
      { name: 'ECDH', public: tempPublicKey },
      myPrivateKeyObj,
      { name: 'AES-GCM', length: AES_KEY_LENGTH },
      false,
      ['decrypt'],
    )
    const plaintext = await window.crypto.subtle.decrypt(
      { name: 'AES-GCM', iv: hexToBytes(ivHex) },
      aesKey,
      hexToBytes(ciphertextHex),
    )

    return new TextDecoder().decode(plaintext)
  } catch {
    return null
  }
}
