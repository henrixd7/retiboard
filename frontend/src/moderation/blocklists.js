/**
 * Shared blocklists support (§13.2).
 *
 * Imported identities are applied through the persistent board-local
 * moderation store. Keyword/plaintext filtering remains frontend-only.
 */

import { useModerationStore } from '../stores/moderationStore.js'

const subscribedLists = new Map()

export async function importBlocklist(boardId, jsonStr) {
  const moderation = useModerationStore()
  try {
    const list = JSON.parse(jsonStr)
    if (!list.issuer || !Array.isArray(list.blocked_identities)) {
      throw new Error('Invalid blocklist format')
    }
    subscribedLists.set(list.issuer, list)
    for (const identityHash of list.blocked_identities) {
      await moderation.blockIdentity(boardId, identityHash)
    }
    return list
  } catch (e) {
    console.warn('Failed to import blocklist:', e)
    return null
  }
}

export function removeBlocklist(issuerHash) {
  subscribedLists.delete(issuerHash)
}

export function getSubscribedLists() {
  return [...subscribedLists.values()]
}
