import { computed, ref, unref } from 'vue'

const CONTENT_SEGMENT_REGEX = /(>>([0-9a-fA-F]{6,64}))|(https?:\/\/[^\s<>"')]+)/g

function resolveReferencedPost(posts, refId) {
  return posts.find((post) => post.post_id === refId || post.post_id.startsWith(refId)) || null
}

function parseLineQuote(line) {
  if (/^>[^>]/.test(line) || line === '>') return 'green'
  if (/^</.test(line)) return 'blue'
  return null
}

export function parseThreadPostContent(text, posts) {
  if (!text) return [{ type: 'text', value: '' }]

  const lines = text.split('\n')
  const segments = []

  for (let lineIndex = 0; lineIndex < lines.length; lineIndex += 1) {
    if (lineIndex > 0) segments.push({ type: 'newline' })

    const line = lines[lineIndex]
    const quote = parseLineQuote(line)
    let lastIndex = 0
    let match

    CONTENT_SEGMENT_REGEX.lastIndex = 0
    while ((match = CONTENT_SEGMENT_REGEX.exec(line)) !== null) {
      if (match.index > lastIndex) {
        segments.push({
          type: 'text',
          value: line.substring(lastIndex, match.index),
          quote,
        })
      }

      if (match[1]) {
        const refId = match[2]
        const target = resolveReferencedPost(posts, refId)
        segments.push({
          type: 'ref',
          id: refId.substring(0, 10),
          fullId: refId,
          exists: Boolean(target),
          postId: target?.post_id || refId,
        })
      } else if (match[3]) {
        segments.push({
          type: 'url',
          value: match[3],
          quote,
        })
      }

      lastIndex = CONTENT_SEGMENT_REGEX.lastIndex
    }

    if (lastIndex < line.length) {
      segments.push({
        type: 'text',
        value: line.substring(lastIndex),
        quote,
      })
    }
  }

  return segments.length ? segments : [{ type: 'text', value: text || '' }]
}

export function useThreadPostText(options) {
  const {
    text,
    posts,
    truncateLimit = 2000,
  } = options

  const expanded = ref(false)

  const textValue = computed(() => unref(text))
  const truncateLimitValue = computed(() => Number(unref(truncateLimit) || 2000))
  const postsValue = computed(() => {
    const resolved = unref(posts)
    return Array.isArray(resolved) ? resolved : []
  })

  const isTruncated = computed(() => {
    return typeof textValue.value === 'string'
      && textValue.value.length > truncateLimitValue.value
      && !expanded.value
  })

  const displayText = computed(() => {
    if (typeof textValue.value !== 'string') return textValue.value
    if (!isTruncated.value) return textValue.value
    return textValue.value.substring(0, truncateLimitValue.value)
  })

  const segments = computed(() => {
    return parseThreadPostContent(displayText.value, postsValue.value)
  })

  function toggleExpand() {
    expanded.value = !expanded.value
  }

  return {
    expanded,
    displayText,
    isTruncated,
    segments,
    toggleExpand,
  }
}
