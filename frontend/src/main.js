import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router/index.js'

/**
 * Capture API Token from URL (§15).
 * 
 * The backend outputs a tokenized URL on startup. We capture it,
 * store it in sessionStorage for the duration of the browser session,
 * and then clear it from the URL using history.replaceState.
 */
const urlParams = new URLSearchParams(window.location.search)
const token = urlParams.get('token')
if (token) {
  sessionStorage.setItem('retiboard_token', token)
  // Clear token from URL immediately.
  const newUrl = window.location.protocol + '//' + window.location.host + window.location.pathname
  window.history.replaceState({ path: newUrl }, '', newUrl)
}

const app = createApp(App)
app.use(createPinia())
app.use(router)

// Global click-outside directive for dropdown menus.
app.directive('click-outside', {
  mounted(el, binding) {
    el._clickOutside = (e) => {
      if (!el.contains(e.target)) {
        binding.value()
      }
    }
    document.addEventListener('click', el._clickOutside, true)
  },
  unmounted(el) {
    document.removeEventListener('click', el._clickOutside, true)
  },
})

app.mount('#app')
