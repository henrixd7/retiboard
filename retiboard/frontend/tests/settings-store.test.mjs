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
  firstStore.showSubscribedBoardLinks = false
  firstStore.setSubscribedBoardOrder(['b-board', 'a-board'])
  firstStore.setQuickLinkBoardIds(['a-board'])

  await new Promise(resolve => setTimeout(resolve, 0))

  assert.equal(
    storageState.get('retiboard:settings:v1'),
    JSON.stringify({
      max_auto_render_bytes: 2 * 1024 * 1024,
      max_manual_load_bytes: 8 * 1024 * 1024,
      show_subscribed_board_links: false,
      subscribed_board_order: ['b-board', 'a-board'],
      quick_link_board_ids: ['a-board'],
    })
  )

  setActivePinia(createPinia())
  const reloadedStore = useSettingsStore()

  assert.equal(reloadedStore.maxAutoRenderBytes, 2 * 1024 * 1024)
  assert.equal(reloadedStore.maxManualLoadBytes, 8 * 1024 * 1024)
  assert.equal(reloadedStore.showSubscribedBoardLinks, false)
  assert.deepEqual(reloadedStore.subscribedBoardOrder, ['b-board', 'a-board'])
  assert.deepEqual(reloadedStore.quickLinkBoardIds, ['a-board'])
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
  assert.equal(store.showSubscribedBoardLinks, true)
  assert.equal(store.quickLinkBoardIds, null)
})

test('quick link ordering and visibility follow configured board preferences', () => {
  installLocalStorage()

  setActivePinia(createPinia())
  const store = useSettingsStore()
  store.setSubscribedBoardOrder(['b', 'a'])
  store.setQuickLinkBoardIds(['a'])

  const boards = [
    { board_id: 'a', display_name: 'alpha' },
    { board_id: 'b', display_name: 'beta' },
    { board_id: 'c', display_name: 'gamma' },
  ]

  store.reconcileBoardPreferences(boards)

  assert.deepEqual(
    store.getOrderedBoards(boards).map((board) => board.board_id),
    ['b', 'a', 'c']
  )
  assert.deepEqual(
    store.getQuickLinkBoards(boards).map((board) => board.board_id),
    ['a']
  )
})
