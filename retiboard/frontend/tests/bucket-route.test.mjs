import test from 'node:test'
import assert from 'node:assert/strict'
import { buildBucketQuery, normalizeBucket } from '../src/composables/useBucketRoute.js'

test('normalizeBucket accepts supported bucket names', () => {
  assert.equal(normalizeBucket('main'), 'main')
  assert.equal(normalizeBucket('hidden'), 'hidden')
  assert.equal(normalizeBucket('banned'), 'banned')
})

test('normalizeBucket falls back to main for invalid values', () => {
  assert.equal(normalizeBucket('unknown'), 'main')
  assert.equal(normalizeBucket(null), 'main')
})

test('buildBucketQuery omits query params for the main bucket', () => {
  assert.deepEqual(buildBucketQuery('main'), {})
  assert.deepEqual(buildBucketQuery('hidden'), { bucket: 'hidden' })
  assert.deepEqual(buildBucketQuery('banned'), { bucket: 'banned' })
})
