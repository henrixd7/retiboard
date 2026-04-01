<script setup>
import { computed, ref } from 'vue'
import { useBoardStore } from '../stores/boardStore.js'

const emit = defineEmits(['select'])
const boardStore = useBoardStore()

const showCreate = ref(false)
const newName = ref('')
const newTextOnly = ref(false)
const creating = ref(false)
const createError = ref(null)
const unsubConfirm = ref(null) // board_id being confirmed for unsub
const expandedDiscovered = ref({})

async function createBoard() {
  if (!newName.value.trim()) return
  creating.value = true
  createError.value = null
  try {
    const board = await boardStore.createBoard({
      display_name: newName.value.trim(),
      text_only: newTextOnly.value,
    })
    newName.value = ''
    showCreate.value = false
    emit('select', board.board_id)
  } catch (e) {
    createError.value = e.message
  } finally {
    creating.value = false
  }
}

async function subscribe(boardId) {
  try {
    await boardStore.subscribeToBoard(boardId)
  } catch (e) {
    alert('Subscribe failed: ' + e.message)
  }
}

async function confirmUnsub(boardId) {
  if (unsubConfirm.value === boardId) {
    // Second click — actually unsubscribe.
    await boardStore.unsubscribe(boardId)
    unsubConfirm.value = null
  } else {
    // First click — show confirmation.
    unsubConfirm.value = boardId
    // Auto-cancel after 3 seconds.
    setTimeout(() => {
      if (unsubConfirm.value === boardId) unsubConfirm.value = null
    }, 3000)
  }
}

function timeAgo(ts) {
  const s = Math.floor(Date.now() / 1000 - ts)
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s/60)}m ago`
  if (s < 86400) return `${Math.floor(s/3600)}h ago`
  return `${Math.floor(s/86400)}d ago`
}

function prettyDuration(seconds) {
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`
  return `${Math.round(seconds / 86400)}d`
}

function compareDiscoveredBoards(a, b) {
  return (
    (b.verified_peer_count || 0) - (a.verified_peer_count || 0) ||
    (b.advertising_peer_count || 0) - (a.advertising_peer_count || 0) ||
    (b.announce_seen_count || 0) - (a.announce_seen_count || 0) ||
    (b.last_seen_at || 0) - (a.last_seen_at || 0) ||
    (a.display_name || '').localeCompare(b.display_name || '') ||
    (a.board_id || '').localeCompare(b.board_id || '')
  )
}

const discoveredGroups = computed(() => {
  const grouped = new Map()
  for (const board of boardStore.discoveredBoards) {
    const key = board.name_key || board.display_name?.trim()?.toLowerCase() || board.board_id
    if (!grouped.has(key)) {
      grouped.set(key, {
        key,
        label: board.display_name,
        items: [],
      })
    }
    grouped.get(key).items.push(board)
  }

  const groups = [...grouped.values()].map((group) => {
    group.items.sort(compareDiscoveredBoards)
    group.best = group.items[0]
    group.label = group.best.display_name
    return group
  })

  groups.sort((a, b) => compareDiscoveredBoards(a.best, b.best))
  return groups
})

const advisoryOrderText = computed(() => {
  const labels = {
    verified_peer_count: 'verified peers',
    advertising_peer_count: 'advertising peers',
    announce_seen_count: 'announce sightings',
    last_seen_at: 'freshness',
  }
  return boardStore.discoveredAdvisoryOrder
    .map((field) => labels[field] || field)
    .join(' -> ')
})

function toggleDiscoveredGroup(key) {
  expandedDiscovered.value = {
    ...expandedDiscovered.value,
    [key]: !expandedDiscovered.value[key],
  }
}

function isDiscoveredExpanded(key) {
  return !!expandedDiscovered.value[key]
}
</script>

<template>
  <div class="selector">
    <!-- Subscribed boards -->
    <section class="section">
      <h2>Subscribed Boards</h2>
      <div v-if="boardStore.boards.length === 0" class="empty">
        No boards yet. Create one or discover from the network.
      </div>
      <div v-else class="board-list">
        <div
          v-for="b in boardStore.boards" :key="b.board_id"
          class="board-card"
        >
          <div class="board-card-main" @click="emit('select', b.board_id)">
            <div class="board-name">
              /{{ b.display_name }}/
              <span v-if="b.text_only" class="badge txt">TXT</span>
            </div>
            <div class="board-meta">
              {{ b.board_id.substring(0, 12) }}…
              · subscribed {{ timeAgo(b.subscribed_at) }}
            </div>
          </div>
          <button
            :class="['btn-unsub', { confirm: unsubConfirm === b.board_id }]"
            @click.stop="confirmUnsub(b.board_id)"
            :title="unsubConfirm === b.board_id ? 'Click again to confirm' : 'Unsubscribe'"
          >
            {{ unsubConfirm === b.board_id ? 'Confirm?' : '✕' }}
          </button>
        </div>
      </div>
    </section>

    <!-- Discovered boards -->
    <section v-if="discoveredGroups.length > 0" class="section">
      <h2>Discovered on Network</h2>
      <p class="discover-hint">
        Advisory order: {{ advisoryOrderText || 'verified peers -> advertising peers -> announce sightings -> freshness' }}.
        Local view only. Stale entries drop after
        {{ boardStore.discoveredStaleAfterSeconds > 0 ? prettyDuration(boardStore.discoveredStaleAfterSeconds) : '12h' }}.
      </p>
      <div class="board-list">
        <div
          v-for="group in discoveredGroups" :key="group.key"
          class="board-card discovered"
        >
          <div class="discover-group-main">
            <div class="discover-group-top">
              <div>
                <div class="board-name">/{{ group.label }}/</div>
                <div class="board-meta">
                  verified {{ group.best.verified_peer_count }}
                  · advertising {{ group.best.advertising_peer_count }}
                  · announces {{ group.best.announce_seen_count }}
                  · seen {{ timeAgo(group.best.last_seen_at) }}
                </div>
              </div>
              <div class="discover-actions">
                <span v-if="group.items.length > 1" class="collision-badge">
                  {{ group.items.length }} variants
                </span>
                <button
                  class="btn-dim btn-sm"
                  @click.stop="toggleDiscoveredGroup(group.key)"
                >
                  {{ isDiscoveredExpanded(group.key) ? 'Hide' : 'Show' }}
                </button>
                <button class="btn-sm" @click.stop="subscribe(group.best.board_id)">
                  {{ group.items.length > 1 ? 'Subscribe best' : 'Subscribe' }}
                </button>
              </div>
            </div>

            <div v-if="isDiscoveredExpanded(group.key)" class="discover-variants">
              <div
                v-for="item in group.items"
                :key="item.board_id"
                class="discover-variant"
              >
                <div class="discover-variant-id">{{ item.board_id.substring(0, 12) }}…</div>
                <div class="discover-variant-meta">
                  owner {{ item.owner_peer_hash ? `${item.owner_peer_hash.substring(0, 12)}…` : 'unknown' }}
                  · verified {{ item.verified_peer_count }}
                  · advertising {{ item.advertising_peer_count }}
                  · announces {{ item.announce_seen_count }}
                  · first {{ timeAgo(item.first_seen_at) }}
                  · last {{ timeAgo(item.last_seen_at) }}
                </div>
                <button class="btn-sm" @click.stop="subscribe(item.board_id)">Subscribe</button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- Create board -->
    <section class="section">
      <button v-if="!showCreate" class="btn" @click="showCreate = true">
        + Create Board
      </button>
      <div v-else class="create-form">
        <input
          v-model="newName" placeholder="Board name…"
          class="input" @keyup.enter="createBoard"
        />
        <label class="checkbox">
          <input type="checkbox" v-model="newTextOnly" /> Text only
        </label>
        <div class="create-actions">
          <button class="btn" @click="createBoard" :disabled="creating">
            {{ creating ? 'Creating…' : 'Create & Announce' }}
          </button>
          <button class="btn-dim" @click="showCreate = false">Cancel</button>
        </div>
        <div v-if="createError" class="error">{{ createError }}</div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.selector { }
.section { margin-bottom: 1.5rem; }
.section h2 { font-size: 0.85rem; color: #808090; font-weight: normal; margin-bottom: 0.5rem; border-bottom: 1px solid #2a2a4a; padding-bottom: 0.3rem; }
.empty { color: #505060; font-size: 0.8rem; font-style: italic; }
.discover-hint { color: #64647a; font-size: 0.72rem; margin: -0.1rem 0 0.7rem; }

.board-list { display: flex; flex-direction: column; gap: 0.4rem; }
.board-card {
  display: flex; align-items: center; gap: 0.5rem;
  padding: 0.6rem 0.8rem; background: #161630; border: 1px solid #2a2a4a;
  border-radius: 3px; transition: border-color 0.15s;
}
.board-card:hover { border-color: #5050a0; }
.board-card-main { flex: 1; cursor: pointer; }
.board-card.discovered { border-style: dashed; cursor: default; }
.discover-group-main { flex: 1; display: flex; flex-direction: column; gap: 0.55rem; }
.discover-group-top {
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: 0.75rem;
}
.discover-actions {
  display: flex; align-items: center; gap: 0.4rem; flex-wrap: wrap;
  justify-content: flex-end;
}
.discover-variants {
  display: flex; flex-direction: column; gap: 0.45rem;
  padding-top: 0.1rem; border-top: 1px solid #242446;
}
.discover-variant {
  display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap;
}
.discover-variant-id {
  color: #b0b0d8; font-size: 0.76rem; min-width: 110px;
}
.discover-variant-meta {
  flex: 1; color: #5d5d74; font-size: 0.7rem;
}
.collision-badge {
  font-size: 0.65rem; color: #d0c890; background: #2b2714;
  border: 1px solid #4f4720; border-radius: 999px; padding: 0.12rem 0.45rem;
}
.board-name { color: #c0c0ff; font-size: 0.9rem; }
.board-meta { color: #505060; font-size: 0.7rem; margin-top: 0.15rem; }
.badge { font-size: 0.6rem; padding: 0.05rem 0.3rem; border-radius: 2px; vertical-align: middle; margin-left: 0.3rem; }
.badge.txt { background: #2a2a5a; color: #a0a0ff; }

.btn-unsub {
  flex-shrink: 0;
  font-family: inherit; font-size: 0.72rem; padding: 0.2rem 0.45rem;
  border-radius: 3px; cursor: pointer;
  border: 1px solid #3a2020; background: transparent; color: #806060;
  transition: all 0.15s;
}
.btn-unsub:hover { border-color: #804040; color: #c06060; background: #1a1020; }
.btn-unsub.confirm {
  border-color: #c04040; color: #ff6060; background: #2a1020;
  font-size: 0.68rem; padding: 0.2rem 0.5rem;
}

.btn, .btn-sm, .btn-dim {
  font-family: inherit; font-size: 0.8rem; padding: 0.35rem 0.8rem;
  border-radius: 3px; cursor: pointer; border: 1px solid #4040a0;
  background: #2a2a5a; color: #c0c0ff;
}
.btn:hover { background: #3a3a6a; }
.btn-sm { font-size: 0.7rem; padding: 0.2rem 0.5rem; margin-top: 0.3rem; }
.btn-dim { background: transparent; color: #606070; border-color: #303040; }
.btn-dim.btn-sm { margin-top: 0; }

.create-form { display: flex; flex-direction: column; gap: 0.5rem; }
.input {
  font-family: inherit; font-size: 0.85rem; padding: 0.4rem 0.6rem;
  background: #0e0e24; border: 1px solid #3a3a5a; border-radius: 3px;
  color: #d0d0e0; outline: none;
}
.input:focus { border-color: #6060c0; }
.checkbox { font-size: 0.78rem; color: #808090; display: flex; align-items: center; gap: 0.3rem; }
.create-actions { display: flex; gap: 0.5rem; }
.error { color: #ff8080; font-size: 0.75rem; }
</style>
