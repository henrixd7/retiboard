import test from 'node:test'
import assert from 'node:assert/strict'

// Polyfill for browser globals in Node test environment
globalThis.sessionStorage = {
  getItem: () => null,
  setItem: () => {},
  removeItem: () => {},
}
globalThis.window = {
  location: {
    pathname: '/',
    href: '',
  },
}

import { ApiError, apiJson, apiJsonResponse, apiOk } from '../src/utils/api.js'

test('apiJson surfaces backend detail as an ApiError message', async () => {
  const originalFetch = globalThis.fetch
  globalThis.fetch = async () => new Response(
    JSON.stringify({ detail: 'Board unavailable' }),
    {
      status: 503,
      headers: { 'Content-Type': 'application/json' },
    },
  )

  try {
    await assert.rejects(
      () => apiJson('/api/status'),
      (error) => {
        assert.equal(error instanceof ApiError, true)
        assert.equal(error.status, 503)
        assert.equal(error.message, 'Board unavailable')
        return true
      },
    )
  } finally {
    globalThis.fetch = originalFetch
  }
})

test('apiJsonResponse can return non-throwing 404 details for callers that branch on status', async () => {
  const originalFetch = globalThis.fetch
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: 'Missing' }), {
    status: 404,
    headers: { 'Content-Type': 'application/json' },
  })

  try {
    const result = await apiJsonResponse('/api/boards/x/threads/y', {
      throwOnError: false,
    })
    assert.equal(result.response.status, 404)
    assert.equal(result.error instanceof ApiError, true)
    assert.equal(result.error.message, 'Missing')
    assert.deepEqual(result.data, { detail: 'Missing' })
  } finally {
    globalThis.fetch = originalFetch
  }
})

test('apiJson turns timed-out requests into timeout ApiErrors', async () => {
  const originalFetch = globalThis.fetch
  globalThis.fetch = async (_url, options = {}) => new Promise((_, reject) => {
    options.signal?.addEventListener('abort', () => {
      reject(new DOMException('The operation was aborted.', 'AbortError'))
    }, { once: true })
  })

  try {
    await assert.rejects(
      () => apiJson('/api/status', { timeoutMs: 5 }),
      (error) => {
        assert.equal(error instanceof ApiError, true)
        assert.equal(error.code, 'timeout')
        assert.equal(error.isTimeout, true)
        return true
      },
    )
  } finally {
    globalThis.fetch = originalFetch
  }
})

test('apiOk returns false for non-2xx control responses without throwing', async () => {
  const originalFetch = globalThis.fetch
  globalThis.fetch = async () => new Response('', { status: 409 })

  try {
    const ok = await apiOk('/api/boards/x/payloads/blob/pause', { method: 'POST' })
    assert.equal(ok, false)
  } finally {
    globalThis.fetch = originalFetch
  }
})
