/**
 * Client-side moderation filters.
 *
 * Structural moderation is persisted per-board through moderationStore.
 * Keyword filters remain session-local and frontend-only.
 */

import { useModerationStore } from '../stores/moderationStore.js'

const keywordPatterns = []  // Array of { pattern: RegExp, label: string }

export function addKeywordFilter(pattern, label = '') {
  try {
    const regex = new RegExp(pattern, 'i')
    keywordPatterns.push({ pattern: regex, label: label || pattern })
  } catch (e) {
    console.warn('Invalid keyword filter pattern:', pattern, e)
  }
}

export function removeKeywordFilter(index) {
  keywordPatterns.splice(index, 1)
}

export function matchesKeywordFilter(text) {
  if (!text) return false
  return keywordPatterns.some(kf => kf.pattern.test(text))
}

export function getKeywordFilters() {
  return keywordPatterns.map((kf, i) => ({
    index: i,
    label: kf.label,
    pattern: kf.pattern.source,
  }))
}

export function shouldHidePost(post, decryptedText = null) {
  const moderation = useModerationStore()
  if (moderation.shouldHidePostStructurally(post)) return true
  if (decryptedText && matchesKeywordFilter(decryptedText)) return true
  return false
}

export function exportFilters() {
  const moderation = useModerationStore()
  return JSON.stringify({
    blocked_identities: [...moderation.blockedIdentities],
    hidden_threads: [...moderation.hiddenThreads],
    hidden_posts: [...moderation.hiddenPosts],
    keyword_patterns: keywordPatterns.map(kf => ({
      pattern: kf.pattern.source,
      label: kf.label,
    })),
  }, null, 2)
}

export async function importFilters(boardId, jsonStr) {
  const moderation = useModerationStore()
  try {
    const data = JSON.parse(jsonStr)
    if (data.blocked_identities) {
      for (const hash of data.blocked_identities) {
        await moderation.blockIdentity(boardId, hash)
      }
    }
    if (data.hidden_threads) {
      for (const threadId of data.hidden_threads) {
        await moderation.hideThread(boardId, threadId)
      }
    }
    if (data.hidden_posts) {
      for (const postId of data.hidden_posts) {
        await moderation.hidePost(boardId, postId)
      }
    }
    if (data.keyword_patterns) {
      data.keyword_patterns.forEach(kf => addKeywordFilter(kf.pattern, kf.label))
    }
  } catch (e) {
    console.warn('Failed to import filters:', e)
  }
}
