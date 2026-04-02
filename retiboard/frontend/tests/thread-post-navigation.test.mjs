import test from 'node:test'
import assert from 'node:assert/strict'
import { getScrollEdgeDirection } from '../src/composables/useThreadPostNavigation.js'

test('scroll edge direction follows actual scroll direction', () => {
  assert.equal(getScrollEdgeDirection(20, 'up'), 'down')
  assert.equal(getScrollEdgeDirection(-20, 'down'), 'up')
  assert.equal(getScrollEdgeDirection(3, 'down'), 'down')
})
