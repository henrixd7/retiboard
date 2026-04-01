import { nextTick, onMounted, onUnmounted, ref, watch } from 'vue'

/**
 * Encapsulates thread-local scroll helpers and hover preview state.
 */
export function useThreadPostNavigation(options) {
  const {
    route,
    router,
    posts,
    postText,
  } = options

  const hoverPreview = ref(null)
  const scrollEdgeDir = ref('down')
  let lastScrollY = 0

  function clearScrollQuery(postId) {
    if (route.query.scrollTo !== postId) return
    const newQuery = { ...route.query }
    delete newQuery.scrollTo
    router.replace({ query: newQuery }).catch(() => {})
  }

  function scrollToPost(postId) {
    const element = document.getElementById(`post-${postId}`)
    if (!element) return

    element.scrollIntoView({ behavior: 'smooth', block: 'center' })
    element.classList.add('highlight')
    setTimeout(() => element.classList.remove('highlight'), 2000)
    clearScrollQuery(postId)
  }

  function scheduleScrollToPost(postId) {
    nextTick(() => {
      window.setTimeout(() => scrollToPost(postId), 300)
    })
  }

  function scrollToEdge() {
    if (scrollEdgeDir.value === 'down') {
      window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' })
      return
    }

    window.scrollTo({ top: 0, behavior: 'smooth' })
  }

  function onWindowScroll() {
    const nextScrollY = window.scrollY
    const delta = nextScrollY - lastScrollY
    if (Math.abs(delta) <= 8) return

    scrollEdgeDir.value = delta > 0 ? 'up' : 'down'
    lastScrollY = nextScrollY
  }

  function showPostPreview(postId, event) {
    const post = posts.value.find(
      (item) => item.post_id === postId || item.post_id.startsWith(postId),
    )
    if (!post) return

    hoverPreview.value = {
      post,
      text: postText.value[post.post_id],
      anchorEl: event.currentTarget || event.target,
    }
  }

  function hidePostPreview() {
    hoverPreview.value = null
  }

  function clearPreviewForPost(postId) {
    if (hoverPreview.value?.post?.post_id === postId) {
      hoverPreview.value = null
    }
  }

  onMounted(() => {
    window.addEventListener('scroll', onWindowScroll, { passive: true })
    if (route.query.scrollTo) scheduleScrollToPost(route.query.scrollTo)
  })

  onUnmounted(() => {
    window.removeEventListener('scroll', onWindowScroll)
  })

  watch(
    () => route.query.scrollTo,
    (nextPostId) => {
      if (!nextPostId) return
      scheduleScrollToPost(nextPostId)
    },
  )

  return {
    hoverPreview,
    scrollEdgeDir,
    scrollToPost,
    scrollToEdge,
    showPostPreview,
    hidePostPreview,
    clearPreviewForPost,
  }
}
