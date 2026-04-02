"""
RetiBoard cryptographic utilities (backend-side).

The backend handles ONLY:
  - PoW solving and verification (§11)
  - content_hash verification (SHA-256 of opaque ciphertext, §6.2)

The backend NEVER handles:
  - Key derivation (HKDF — frontend only, §5)
  - Encryption/decryption (AES-GCM — frontend only, §5)
  - key_material (never stored in DB, never imported here)
"""
