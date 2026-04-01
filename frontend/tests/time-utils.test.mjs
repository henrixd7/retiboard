import test from 'node:test'
import assert from 'node:assert/strict'
import { timeAgo } from '../src/utils/time.js'

test('timeAgo formats seconds, minutes, hours, and days', () => {
  const originalNow = Date.now
  Date.now = () => 1_000_000 * 1000

  try {
    assert.equal(timeAgo(1_000_000 - 12), '12s')
    assert.equal(timeAgo(1_000_000 - 90), '1m')
    assert.equal(timeAgo(1_000_000 - 7_200), '2h')
    assert.equal(timeAgo(1_000_000 - 172_800), '2d')
  } finally {
    Date.now = originalNow
  }
})
