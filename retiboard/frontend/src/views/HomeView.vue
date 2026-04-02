<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { useBoardStore } from '../stores/boardStore.js'
import { useSettingsStore } from '../stores/settingsStore.js'
import BoardSelector from '../components/BoardSelector.vue'
import BoardQuickNavSettings from '../components/BoardQuickNavSettings.vue'
import NotificationBell from '../components/NotificationBell.vue'
import NetworkStatusIndicator from '../components/NetworkStatusIndicator.vue'
import { apiJson } from '../utils/api.js'

const router = useRouter()
const boardStore = useBoardStore()
const settings = useSettingsStore()
const status = ref(null)
const showSettings = ref(false)
let pollTimer = null

async function refreshStatus() {
  try {
    status.value = await apiJson('/api/status', { cache: 'no-store' })
  } catch {}
}

// Slider works on a log scale for intuitive feel across wide range.
// Slider range: 0-100. Maps to 0 → 1KB → ... → ~1GB exponentially.
const sliderValue = computed({
  get() {
    if (settings.maxAutoRenderBytes <= 0) return 0
    // log scale: slider 1-100 maps to 1KB - 1GB
    const bytes = settings.maxAutoRenderBytes
    const minLog = Math.log(1024)           // 1 KB
    const maxLog = Math.log(1024 * 1024 * 1024) // 1 GB
    const val = ((Math.log(bytes) - minLog) / (maxLog - minLog)) * 100
    return Math.max(0, Math.min(100, Math.round(val)))
  },
  set(v) {
    if (v <= 0) {
      settings.maxAutoRenderBytes = 0
      return
    }
    const minLog = Math.log(1024)
    const maxLog = Math.log(1024 * 1024 * 1024)
    const bytes = Math.exp(minLog + (v / 100) * (maxLog - minLog))
    settings.maxAutoRenderBytes = Math.round(bytes)
  }
})

// Direct input in KB for precise control.
const inputKB = computed({
  get() {
    return Math.round(settings.maxAutoRenderBytes / 1024)
  },
  set(v) {
    const kb = parseInt(v, 10)
    settings.maxAutoRenderBytes = (isNaN(kb) || kb < 0) ? 0 : kb * 1024
  }
})

onMounted(async () => {
  await boardStore.fetchBoards()
  await boardStore.fetchDiscovered()
  await refreshStatus()

  pollTimer = setInterval(async () => {
    await boardStore.fetchDiscovered()
    await refreshStatus()
  }, 10_000)
})

onUnmounted(() => { if (pollTimer) clearInterval(pollTimer) })

function openBoard(boardId) {
  boardStore.selectBoard(boardId)
  router.push({ name: 'catalog', params: { boardId } })
}
</script>

<template>
  <div class="home">
    <header class="home-header">
      <h1><span class="logo">◈</span> RetiBoard <span class="ver">v3.6.2</span></h1>
      <p class="tagline">Sovereign · Ephemeral · Opaque</p>
    </header>

    <div v-if="status" class="node-info">
      <div class="node-info-center">
        <span class="dot online"></span>
        Node online · {{ status.boards_subscribed }} board(s)
        <NetworkStatusIndicator class="home-net-status" />
        <button class="settings-toggle" @click="showSettings = !showSettings" title="Settings">⚙</button>
      </div>
      <NotificationBell class="node-info-bell" />
    </div>

    <!-- Settings panel -->
    <div v-if="showSettings" class="settings-panel">
      <h3>Settings</h3>
      
      <div class="setting-row">
        <label>Global Storage Quota</label>
        <p class="setting-hint">
          Maximum disk space to use for all boards combined. When exceeded, the oldest content
          across all boards will be purged regardless of its individual TTL.
        </p>
        <div class="threshold-controls">
          <div class="threshold-input-row">
            <input
              type="number" min="100" step="100"
              v-model.number="settings.globalStorageLimitMB"
              class="threshold-input"
            />
            <span class="threshold-unit">MB</span>
            <span class="threshold-display">
              = {{ (settings.globalStorageLimitMB / 1024).toFixed(1) }} GB
            </span>
          </div>
          <div class="quick-presets">
            <button class="qp" @click="settings.globalStorageLimitMB = 500">500 MB</button>
            <button class="qp" @click="settings.globalStorageLimitMB = 1024">1 GB</button>
            <button class="qp" @click="settings.globalStorageLimitMB = 2048">2 GB</button>
            <button class="qp" @click="settings.globalStorageLimitMB = 5120">5 GB</button>
            <button class="qp" @click="settings.globalStorageLimitMB = 10240">10 GB</button>
          </div>
        </div>
      </div>

      <div class="setting-row">
        <label>Auto-load file threshold</label>
        <p class="setting-hint">
          Attached files larger than this are not automatically downloaded.
          Text content always loads regardless. Set to 0 to never auto-load files.
        </p>

        <div class="threshold-controls">
          <input
            type="range" min="0" max="100" step="1"
            v-model.number="sliderValue"
            class="threshold-slider"
          />
          <div class="threshold-input-row">
            <input
              type="number" min="0"
              v-model.number="inputKB"
              class="threshold-input"
            />
            <span class="threshold-unit">KB</span>
            <span class="threshold-display">
              = {{ settings.maxAutoRenderBytes <= 0 ? 'Off' : settings.prettySize(settings.maxAutoRenderBytes) }}
            </span>
          </div>

          <div class="quick-presets">
            <button class="qp" @click="settings.maxAutoRenderBytes = 0">Off</button>
            <button class="qp" @click="settings.maxAutoRenderBytes = 64 * 1024">64 KB</button>
            <button class="qp" @click="settings.maxAutoRenderBytes = 512 * 1024">512 KB</button>
            <button class="qp" @click="settings.maxAutoRenderBytes = 2 * 1024 * 1024">2 MB</button>
            <button class="qp" @click="settings.maxAutoRenderBytes = 10 * 1024 * 1024">10 MB</button>
            <button class="qp" @click="settings.maxAutoRenderBytes = 100 * 1024 * 1024">100 MB</button>
          </div>
        </div>
      </div>

      <div class="setting-row">
        <label>Subscribed board links</label>
        <p class="setting-hint">
          Show a 4chan-style board strip on catalog and thread pages. Choose which subscribed boards appear and drag to change their order.
        </p>
        <BoardQuickNavSettings />
      </div>
    </div>

    <BoardSelector @select="openBoard" />

    <footer class="home-footer">
      All data is local. board links and metadata remain transparent; nothing persists beyond its TTL.
    </footer>
  </div>
</template>

<style scoped>
.home { max-width: 800px; margin: 0 auto; padding: 2rem 1rem; }
.home-header { text-align: center; margin-bottom: 1.5rem; }
.home-header h1 { font-size: 1.6rem; font-weight: normal; color: #c0c0ff; }
.logo { color: #7070ff; }
.ver { font-size: 0.65rem; color: #505060; vertical-align: super; }
.tagline { color: #606070; font-size: 0.8rem; letter-spacing: 0.12em; margin-top: 0.3rem; }
.node-info {
  position: relative; font-size: 0.78rem; color: #80ff80;
  background: #0a1a0a; padding: 0.4rem 0.8rem; border-radius: 4px;
  margin-bottom: 1.5rem; display: flex; align-items: center;
  justify-content: center;
}
.node-info-center {
  display: flex; align-items: center; justify-content: center; gap: 0.4rem;
}
.node-info-bell {
  position: absolute; right: 0.6rem; top: 50%; transform: translateY(-50%);
}
.home-net-status {
  background: rgba(0, 0, 0, 0.3) !important;
  margin-left: 0.5rem;
}
.dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
.dot.online { background: #40c040; }
.settings-toggle {
  background: none; border: none; color: #608060; cursor: pointer;
  font-size: 1rem; padding: 0 0.3rem; margin-left: 0.5rem;
}
.settings-toggle:hover { color: #a0ffa0; }

.settings-panel {
  background: #12122a; border: 1px solid #2a2a4a; border-radius: 4px;
  padding: 1rem; margin-bottom: 1.5rem;
}
.settings-panel h3 {
  font-size: 0.9rem; font-weight: normal; color: #a0a0c0;
  margin-bottom: 0.8rem; border-bottom: 1px solid #2a2a4a; padding-bottom: 0.4rem;
}
.setting-row { padding-bottom: 1rem; margin-bottom: 1rem; border-bottom: 1px solid #222240; }
.setting-row:last-child { padding-bottom: 0; margin-bottom: 0; border-bottom: none; }
.setting-row label { font-size: 0.8rem; color: #9090b0; display: block; margin-bottom: 0.3rem; }
.setting-hint { font-size: 0.7rem; color: #505060; margin-bottom: 0.6rem; }

.threshold-controls { display: flex; flex-direction: column; gap: 0.5rem; }
.threshold-slider {
  width: 100%; accent-color: #6060c0; cursor: pointer;
}
.threshold-input-row {
  display: flex; align-items: center; gap: 0.4rem;
}
.threshold-input {
  width: 90px; font-family: inherit; font-size: 0.8rem;
  background: #0e0e24; border: 1px solid #3a3a5a; border-radius: 3px;
  color: #c0c0d0; padding: 0.25rem 0.4rem; text-align: right;
}
.threshold-input:focus { border-color: #6060c0; outline: none; }
.threshold-unit { font-size: 0.75rem; color: #606080; }
.threshold-display { font-size: 0.75rem; color: #8080a0; margin-left: auto; }

.quick-presets { display: flex; flex-wrap: wrap; gap: 0.3rem; }
.qp {
  font-family: inherit; font-size: 0.68rem; padding: 0.2rem 0.5rem;
  border-radius: 3px; cursor: pointer;
  border: 1px solid #303050; background: #1a1a30; color: #7070a0;
}
.qp:hover { border-color: #5050a0; color: #a0a0ff; }

.prune-presets { display: flex; flex-wrap: wrap; gap: 0.3rem; margin-bottom: 0.4rem; }
.prune-custom-row { display: flex; align-items: center; gap: 0.4rem; }
.qp.active { border-color: #5050a0; color: #c0c0ff; background: #1e1e3a; }

.home-footer {
  text-align: center; margin-top: 2rem; padding-top: 1rem;
  border-top: 1px solid #2a2a4a; color: #404050; font-size: 0.7rem;
}
</style>
