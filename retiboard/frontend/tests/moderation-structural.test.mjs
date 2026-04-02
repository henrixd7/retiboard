import test from 'node:test'
import assert from 'node:assert/strict'
import {
  getPostBucketFromState,
  getPostHiddenReasonFromState,
  getPostReasonFromState,
  getThreadBucketFromState,
  getThreadHiddenReasonFromState,
  getThreadReasonFromState,
  shouldHidePostFromState,
  shouldHideThreadFromState,
} from '../src/moderation/structural.js'

function makeState(overrides = {}) {
  return {
    blockedIdentities: new Set(),
    hiddenThreads: new Set(),
    hiddenPosts: new Set(),
    purgedThreads: new Set(),
    purgedPosts: new Set(),
    allowedThreadExceptions: new Set(),
    allowedPostExceptions: new Set(),
    ...overrides,
  }
}

test('thread reason precedence is purged > hidden', () => {
  const thread = { thread_id: 't1' }
  assert.equal(getThreadReasonFromState(makeState(), thread), null)
  assert.equal(
    getThreadReasonFromState(makeState({ hiddenThreads: new Set(['t1']) }), thread),
    'hidden_thread'
  )
  assert.equal(
    getThreadReasonFromState(makeState({ purgedThreads: new Set(['t1']), hiddenThreads: new Set(['t1']) }), thread),
    'purged'
  )
})

test('post reason precedence mirrors backend policy ordering', () => {
  const post = { post_id: 'p1', thread_id: 't1' }
  assert.equal(
    getPostReasonFromState(makeState({ purgedThreads: new Set(['t1']) }), post),
    'purged_thread'
  )
  assert.equal(
    getPostReasonFromState(makeState({ hiddenThreads: new Set(['t1']) }), post),
    'hidden_thread'
  )
  assert.equal(
    getPostReasonFromState(makeState({ purgedPosts: new Set(['p1']) }), post),
    'purged_post'
  )
  assert.equal(
    getPostReasonFromState(makeState({ hiddenPosts: new Set(['p1']) }), post),
    'hidden_post'
  )
})

test('session exceptions suppress structural hiding without mutating persistent state', () => {
  const thread = { thread_id: 't1' }
  const post = { post_id: 'p1', thread_id: 't1' }
  const state = makeState({
    allowedThreadExceptions: new Set(['t1']),
    allowedPostExceptions: new Set(['p1']),
  })
  assert.equal(shouldHideThreadFromState(state, thread), false)
  assert.equal(shouldHidePostFromState(state, post), false)
})
