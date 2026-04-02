<script setup>
import { computed, watch } from 'vue'
import { useBoardStore } from '../stores/boardStore.js'
import { useSettingsStore } from '../stores/settingsStore.js'

const props = defineProps({
  currentBoardId: {
    type: String,
    default: '',
  },
})

const boardStore = useBoardStore()
const settings = useSettingsStore()

watch(
  () => boardStore.boards,
  (boards) => {
    settings.reconcileBoardPreferences(boards)
  },
  { immediate: true },
)

const quickLinkBoards = computed(() => {
  return settings.getQuickLinkBoards(boardStore.boards)
})
</script>

<template>
  <nav v-if="quickLinkBoards.length" class="board-quick-nav" aria-label="Subscribed boards">
    <router-link to="/" class="board-quick-nav-link board-quick-nav-home">boards</router-link>
    <span class="board-quick-nav-separator">[</span>
    <template v-for="(board, index) in quickLinkBoards" :key="board.board_id">
      <router-link
        class="board-quick-nav-link"
        :class="{ active: board.board_id === currentBoardId }"
        :to="{ name: 'catalog', params: { boardId: board.board_id } }"
      >/{{ board.display_name }}/</router-link>
      <span v-if="index < quickLinkBoards.length - 1" class="board-quick-nav-separator">/</span>
    </template>
    <span class="board-quick-nav-separator">]</span>
  </nav>
</template>

<style scoped>
.board-quick-nav {
  display: flex;
  flex-wrap: wrap;
  gap: 0.35rem;
  align-items: center;
  margin: 0 0 0.8rem;
  padding: 0.35rem 0.55rem;
  border: 1px solid #2c3146;
  border-radius: 3px;
  background: #111626;
  font-size: 0.74rem;
}

.board-quick-nav-link {
  color: #8da4d8;
}

.board-quick-nav-link:hover {
  color: #c4d4ff;
}

.board-quick-nav-link.active {
  color: #f0f4ff;
  font-weight: 700;
}

.board-quick-nav-home {
  color: #7bb6a2;
  text-transform: lowercase;
}

.board-quick-nav-separator {
  color: #52607f;
}
</style>
