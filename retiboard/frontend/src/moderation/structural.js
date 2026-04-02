/**
 * Pure structural moderation helpers.
 *
 * No Vue/Pinia imports here. This keeps the decision logic testable with the
 * built-in Node test runner and mirrors the backend policy ordering.
 */

function _identityHash(item) {
  return item?.identity_hash || item?.op_identity_hash || ''
}

export function getThreadReasonFromState(state, thread) {
  if (!state || !thread) return null
  const ih = _identityHash(thread)
  if (ih && state.blockedIdentities?.has(ih)) return 'blocked_identity'
  if (state.purgedThreads?.has(thread.thread_id)) return 'purged'
  return getThreadHiddenReasonFromState(state, thread)
}

export function getPostReasonFromState(state, post) {
  if (!state || !post) return null
  const ih = _identityHash(post)
  if (ih && state.blockedIdentities?.has(ih)) return 'blocked_identity'
  if (state.purgedThreads?.has(post.thread_id)) return 'purged_thread'
  if (state.purgedPosts?.has(post.post_id)) return 'purged_post'
  return getPostHiddenReasonFromState(state, post)
}

export function getThreadHiddenReasonFromState(state, thread) {
  if (!state || !thread) return null
  if (state.hiddenThreads?.has(thread.thread_id)) return 'hidden_thread'
  if (state.hiddenPosts?.has(thread.thread_id)) return 'hidden_post'
  const ih = _identityHash(thread)
  if (ih && state.hiddenIdentities?.has(ih)) return 'hidden_identity'
  return null
}

export function getPostHiddenReasonFromState(state, post) {
  if (!state || !post) return null
  if (state.hiddenThreads?.has(post.thread_id)) return 'hidden_thread'
  if (state.hiddenPosts?.has(post.post_id)) return 'hidden_post'
  const ih = _identityHash(post)
  if (ih && state.hiddenIdentities?.has(ih)) return 'hidden_identity'
  return null
}

export function getThreadBucketFromState(state, thread) {
  if (!thread) return 'main'
  if (state?.allowedThreadExceptions?.has(thread.thread_id)) return 'main'
  const reason = getThreadReasonFromState(state, thread)
  if (reason === 'blocked_identity') return 'banned'
  // Check hidden state independently: a purged thread whose stub has been
  // hidden should still appear in the hidden bucket, not main.
  // Purge takes precedence for content serving, but the placeholder visibility
  // follows the hidden rule — user explicitly moved it to hidden bucket.
  const hiddenReason = getThreadHiddenReasonFromState(state, thread)
  if (hiddenReason) return 'hidden'
  return 'main'
}

export function getPostBucketFromState(state, post) {
  if (!post) return 'main'
  if (state?.allowedThreadExceptions?.has(post.thread_id)) return 'main'
  if (state?.allowedPostExceptions?.has(post.post_id)) return 'main'
  const reason = getPostReasonFromState(state, post)
  if (reason === 'blocked_identity') return 'banned'
  // Same as threads: hidden state is orthogonal to purge state.
  const hiddenReason = getPostHiddenReasonFromState(state, post)
  if (hiddenReason) return 'hidden'
  return 'main'
}

export function shouldHideThreadFromState(state, thread) {
  return getThreadBucketFromState(state, thread) === 'hidden'
}

export function shouldHidePostFromState(state, post) {
  return getPostBucketFromState(state, post) === 'hidden'
}
