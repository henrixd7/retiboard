import { computed } from 'vue'

const VALID_BUCKETS = new Set(['main', 'hidden', 'banned'])

export function normalizeBucket(bucket) {
  const raw = String(bucket || 'main')
  return VALID_BUCKETS.has(raw) ? raw : 'main'
}

export function buildBucketQuery(bucket) {
  const normalized = normalizeBucket(bucket)
  return normalized === 'main' ? {} : { bucket: normalized }
}

/**
 * Shared bucket-route state for catalog/thread views.
 */
export function useBucketRoute({
  route,
  router,
  routeName,
  getParams,
}) {
  const currentBucket = computed(() => normalizeBucket(route.query.bucket))
  const isHiddenBucket = computed(() => currentBucket.value === 'hidden')
  const isBannedBucket = computed(() => currentBucket.value === 'banned')
  const currentBucketQuery = computed(() => buildBucketQuery(currentBucket.value))

  function resolveParams() {
    return typeof getParams === 'function' ? getParams() : (getParams || {})
  }

  function queryForBucket(bucket = currentBucket.value) {
    return buildBucketQuery(bucket)
  }

  function setBucket(bucket) {
    return router.push({
      name: routeName,
      params: resolveParams(),
      query: queryForBucket(bucket),
    })
  }

  return {
    currentBucket,
    currentBucketQuery,
    isHiddenBucket,
    isBannedBucket,
    queryForBucket,
    setBucket,
  }
}
