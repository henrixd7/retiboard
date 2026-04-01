/**
 * Vue Router configuration for RetiBoard.
 *
 * Routes:
 *   /                         — Board selector (home)
 *   /board/:boardId           — Catalog view (thread grid)
 *   /board/:boardId/thread/:threadId — Thread view
 */
import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  {
    path: '/',
    name: 'home',
    component: () => import('../views/HomeView.vue'),
  },
  {
    path: '/board/:boardId',
    name: 'catalog',
    component: () => import('../views/CatalogView.vue'),
    props: true,
  },
  {
    path: '/board/:boardId/thread/:threadId',
    name: 'thread',
    component: () => import('../views/ThreadView.vue'),
    props: true,
  },
  {
    path: '/unauthorized',
    name: 'unauthorized',
    component: () => import('../views/UnauthorizedView.vue'),
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

/**
 * Global navigation guard: check for API token.
 * 
 * If no token is found in sessionStorage, redirect to the unauthorized view.
 * The token is captured and stored in main.js from the initial URL.
 */
router.beforeEach((to, from, next) => {
  if (to.name === 'unauthorized') {
    return next()
  }

  const token = sessionStorage.getItem('retiboard_token')
  if (!token) {
    return next({ name: 'unauthorized' })
  }

  next()
})

export default router
