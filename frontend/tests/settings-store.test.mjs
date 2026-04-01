import test from 'node:test'
import assert from 'node:assert/strict'
import { createPinia, setActivePinia } from 'pinia'
import { useSettingsStore } from '../src/stores/settingsStore.js'

function installLocalStorage(initialState = {}) {
  const state = new Map(Object.entries(initialState))
  const localStorage = {
    getItem(key) {
      return state.has(key) ? state.get(key) : null
    },
    setItem(key, value) {
      state.set(key, String(value))
    },
    removeItem(key) {
      state.delete(key)
    },
    clear() {
      state.clear()
    },
  }

  globalThis.window = { localStorage }
  globalThis.localStorage = localStorage
  return state
}

test('settings persist across store recreation via browser localStorage', async () => {
  const storageState = installLocalStorage()

  setActivePinia(createPinia())
  const firstStore = useSettingsStore()
  firstStore.maxAutoRenderBytes = 2 * 1024 * 1024
  firstStore.maxManualLoadBytes = 8 * 1024 * 1024

  await new Promise(resolve => setTimeout(resolve, 0))

  assert.equal(
    storageState.get('retiboard:settings:v1'),
    JSON.stringify({
      max_auto_render_bytes: 2 * 1024 * 1024,
      max_manual_load_bytes: 8 * 1024 * 1024,
    })
  )

  setActivePinia(createPinia())
  const reloadedStore = useSettingsStore()

  assert.equal(reloadedStore.maxAutoRenderBytes, 2 * 1024 * 1024)
  assert.equal(reloadedStore.maxManualLoadBytes, 8 * 1024 * 1024)
})

test('settings hydration ignores malformed persisted values', () => {
  installLocalStorage({
    'retiboard:settings:v1': JSON.stringify({
      max_auto_render_bytes: -1,
      max_manual_load_bytes: 'invalid',
    }),
  })

  setActivePinia(createPinia())
  const store = useSettingsStore()

  assert.equal(store.maxAutoRenderBytes, 512 * 1024)
  assert.equal(store.maxManualLoadBytes, 0)
})
