import { computed } from 'vue'

function buildPurgedStub(stub, threadId) {
  return {
    post_id: stub.post_id,
    thread_id: stub.thread_id || threadId,
    identity_hash: stub.identity_hash || '',
    timestamp: stub.timestamp || 0,
    parent_id: stub.parent_id || null,
    _isStub: true,
  }
}

/**
 * Thread-visible posts are derived from live API data plus session-local
 * purged stubs so tombstones remain undoable after local deletion.
 */
export function useThreadVisiblePosts(options) {
  const {
    posts,
    threadId,
    currentBucket,
    postBucket,
    moderation,
  } = options

  const visiblePosts = computed(() => {
    const livePosts = Array.isArray(posts.value) ? posts.value : []
    const liveIds = new Set(livePosts.map((post) => post.post_id))
    const filteredById = new Map()

    for (const post of livePosts) {
      if (postBucket(post) !== currentBucket.value) continue
      filteredById.set(post.post_id, post)
    }

    for (const stub of moderation.purgedPostStubs.values()) {
      if (!stub || stub.thread_id !== threadId()) continue
      if (liveIds.has(stub.post_id)) continue

      const stubPost = buildPurgedStub(stub, threadId())
      if (postBucket(stubPost) !== currentBucket.value) continue

      filteredById.set(stubPost.post_id, stubPost)
    }

    return [...filteredById.values()].sort(
      (left, right) => (left.timestamp || 0) - (right.timestamp || 0),
    )
  })

  function dropReconciledPurgedPostStubs(fetchedPosts) {
    const fetchedIds = new Set((fetchedPosts || []).map((post) => post.post_id))
    for (const [postId] of moderation.purgedPostStubs) {
      if (fetchedIds.has(postId)) moderation.dropPurgedPostStub(postId)
    }
  }

  return {
    visiblePosts,
    dropReconciledPurgedPostStubs,
  }
}
