import { onUnmounted } from 'vue'

/**
 * Shared delayed refetch loop used after moderation actions that rely on
 * gossip to make restored content visible again.
 */
export function useDelayedRefetch({
  refetch,
  shouldRetry,
  delayMs = 3000,
  maxAttempts = 10,
}) {
  let refetchTimer = null

  function clearRefetch() {
    if (refetchTimer) {
      clearTimeout(refetchTimer)
      refetchTimer = null
    }
  }

  function scheduleRefetch(attempt = 0) {
    clearRefetch()
    if (attempt >= maxAttempts) return

    refetchTimer = setTimeout(async () => {
      await refetch()
      if (shouldRetry()) scheduleRefetch(attempt + 1)
    }, delayMs)
  }

  onUnmounted(clearRefetch)

  return {
    clearRefetch,
    scheduleRefetch,
  }
}
