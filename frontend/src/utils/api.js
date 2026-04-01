const DEFAULT_TIMEOUT_MS = 12_000

export class ApiError extends Error {
  constructor(message, options = {}) {
    super(message, options.cause ? { cause: options.cause } : undefined)
    this.name = 'ApiError'
    this.status = options.status ?? null
    this.url = options.url || ''
    this.method = options.method || 'GET'
    this.code = options.code || (this.status ? `http_${this.status}` : 'request_failed')
    this.details = options.details ?? null
    this.isTimeout = this.code === 'timeout'
    this.isAborted = this.code === 'aborted'
  }
}

function parseJsonText(raw) {
  if (!raw) return null
  try {
    return JSON.parse(raw)
  } catch {
    return null
  }
}

async function parseResponseData(response, parseAs) {
  if (parseAs === 'response') return null

  if (parseAs === 'text') {
    return response.text()
  }

  if ([204, 205, 304].includes(response.status)) return null

  const raw = await response.text()
  if (!raw) return null

  if (parseAs === 'json') {
    return parseJsonText(raw)
  }

  return raw
}

function extractErrorMessage(response, data) {
  if (typeof data === 'string' && data.trim()) return data.trim()
  if (data && typeof data === 'object') {
    if (typeof data.detail === 'string' && data.detail.trim()) return data.detail.trim()
    if (typeof data.message === 'string' && data.message.trim()) return data.message.trim()
    if (typeof data.error === 'string' && data.error.trim()) return data.error.trim()
  }
  return `HTTP ${response.status}`
}

function buildAbortError(url, method, timeoutMs, signal, cause) {
  const wasCallerAbort = !!signal?.aborted
  const message = wasCallerAbort ? 'Request aborted' : `Request timed out after ${timeoutMs} ms`
  return new ApiError(message, {
    url,
    method,
    code: wasCallerAbort ? 'aborted' : 'timeout',
    cause,
  })
}

function buildNetworkError(url, method, cause) {
  return new ApiError('Network request failed', {
    url,
    method,
    code: 'network_error',
    cause,
  })
}

export async function apiRequest(url, options = {}) {
  const {
    timeoutMs = DEFAULT_TIMEOUT_MS,
    parseAs = 'json',
    throwOnError = true,
    signal,
    ...fetchOptions
  } = options
  const method = String(fetchOptions.method || 'GET').toUpperCase()
  const controller = new AbortController()
  let timeoutId = null
  let abortListener = null

  if (signal?.aborted) {
    controller.abort(signal.reason)
  } else if (signal) {
    abortListener = () => controller.abort(signal.reason)
    signal.addEventListener('abort', abortListener, { once: true })
  }

  if (timeoutMs > 0) {
    timeoutId = setTimeout(() => controller.abort(), timeoutMs)
  }

  // Inject ephemeral API token if present in sessionStorage (§15).
  const token = sessionStorage.getItem('retiboard_token')
  const headers = new Headers(fetchOptions.headers || {})
  if (token) {
    headers.set('X-RetiBoard-Token', token)
  }

  let response = null
  let data = null

  try {
    response = await fetch(url, {
      ...fetchOptions,
      headers,
      signal: controller.signal,
    })
    data = await parseResponseData(response, parseAs)
  } catch (error) {
    const apiError = error?.name === 'AbortError'
      ? buildAbortError(url, method, timeoutMs, signal, error)
      : buildNetworkError(url, method, error)

    if (throwOnError) throw apiError
    return { response: null, data: null, error: apiError }
  } finally {
    if (timeoutId) clearTimeout(timeoutId)
    if (signal && abortListener) signal.removeEventListener('abort', abortListener)
  }

  if (!response.ok) {
    // 401 Unauthorized: clear token and redirect to unauthorized page.
    if (response.status === 401) {
      sessionStorage.removeItem('retiboard_token')
      if (window.location.pathname !== '/unauthorized') {
        window.location.href = '/unauthorized'
      }
    }

    const apiError = new ApiError(extractErrorMessage(response, data), {
      status: response.status,
      url,
      method,
      details: data,
    })
    if (throwOnError) throw apiError
    return { response, data, error: apiError }
  }

  return { response, data, error: null }
}

export async function apiJson(url, options = {}) {
  const { data } = await apiRequest(url, {
    ...options,
    parseAs: 'json',
  })
  return data
}

export async function apiJsonResponse(url, options = {}) {
  return apiRequest(url, {
    ...options,
    parseAs: 'json',
  })
}

export async function apiOk(url, options = {}) {
  const { response } = await apiRequest(url, {
    ...options,
    parseAs: 'response',
    throwOnError: false,
  })
  return !!response?.ok
}
