<script setup>
import { computed, ref, watch } from 'vue'
import { useBoardStore } from '../stores/boardStore.js'
import { useSettingsStore } from '../stores/settingsStore.js'

const boardStore = useBoardStore()
const settings = useSettingsStore()
const dragBoardId = ref('')
const dragOverBoardId = ref('')

const orderedBoards = computed(() => {
  return settings.getOrderedBoards(boardStore.boards)
})

const visibleBoardIds = computed(() => {
  if (settings.quickLinkBoardIds === null) {
    return new Set(orderedBoards.value.map((board) => board.board_id))
  }
  return new Set(settings.quickLinkBoardIds)
})

watch(
  () => boardStore.boards,
  (boards) => {
    settings.reconcileBoardPreferences(boards)
  },
  { immediate: true },
)

function onToggleAll(event) {
  if (event.target.checked) {
    settings.setQuickLinkBoardIds(null)
    return
  }
  settings.setQuickLinkBoardIds([])
}

function onToggleBoard(boardId, checked) {
  const nextVisible = [...visibleBoardIds.value]
  if (checked) {
    if (!nextVisible.includes(boardId)) nextVisible.push(boardId)
  } else {
    const index = nextVisible.indexOf(boardId)
    if (index !== -1) nextVisible.splice(index, 1)
  }
  settings.setQuickLinkBoardIds(nextVisible)
}

function onDragStart(boardId) {
  dragBoardId.value = boardId
}

function onDragEnter(boardId) {
  if (!dragBoardId.value || dragBoardId.value === boardId) return
  dragOverBoardId.value = boardId
}

function onDrop(boardId) {
  if (!dragBoardId.value || dragBoardId.value === boardId) {
    dragBoardId.value = ''
    dragOverBoardId.value = ''
    return
  }

  const targetIndex = settings.subscribedBoardOrder.indexOf(boardId)
  if (targetIndex !== -1) {
    settings.moveSubscribedBoard(dragBoardId.value, targetIndex)
  }
  dragBoardId.value = ''
  dragOverBoardId.value = ''
}

function onDragEnd() {
  dragBoardId.value = ''
  dragOverBoardId.value = ''
}
</script>

<template>
  <div class="quick-nav-settings">
    <label class="checkbox">
      <input v-model="settings.showSubscribedBoardLinks" type="checkbox" />
      Show subscribed board links on board pages
    </label>

    <label class="checkbox">
      <input
        :checked="settings.quickLinkBoardIds === null"
        type="checkbox"
        @change="onToggleAll"
      />
      Show all subscribed boards
    </label>

    <div class="quick-nav-list" role="listbox" aria-label="Subscribed board quick links">
      <div
        v-for="board in orderedBoards"
        :key="board.board_id"
        class="quick-nav-row"
        :class="{ 'drag-over': dragOverBoardId === board.board_id }"
        draggable="true"
        @dragstart="onDragStart(board.board_id)"
        @dragenter.prevent="onDragEnter(board.board_id)"
        @dragover.prevent
        @drop.prevent="onDrop(board.board_id)"
        @dragend="onDragEnd"
      >
        <span class="drag-handle" title="Drag to reorder">⋮⋮</span>
        <label class="quick-nav-board">
          <input
            :checked="visibleBoardIds.has(board.board_id)"
            type="checkbox"
            @change="onToggleBoard(board.board_id, $event.target.checked)"
          />
          <span>/{{ board.display_name }}/</span>
        </label>
        <span class="quick-nav-id">{{ board.board_id.substring(0, 10) }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.quick-nav-settings {
  display: flex;
  flex-direction: column;
  gap: 0.55rem;
}

.checkbox {
  font-size: 0.78rem;
  color: #9090b0;
  display: flex;
  align-items: center;
  gap: 0.45rem;
}

.quick-nav-list {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  max-height: 250px;
  overflow-y: auto;
  padding: 0.45rem;
  border: 1px solid #2a2f45;
  border-radius: 3px;
  background: #0e1220;
}

.quick-nav-row {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  padding: 0.45rem 0.55rem;
  border: 1px solid #24293d;
  border-radius: 3px;
  background: #141a2a;
}

.quick-nav-row.drag-over {
  border-color: #5a78c2;
  background: #182138;
}

.drag-handle {
  color: #64759d;
  cursor: grab;
  user-select: none;
  letter-spacing: -0.15em;
}

.quick-nav-board {
  display: flex;
  align-items: center;
  gap: 0.45rem;
  flex: 1;
  color: #c0c9e8;
  font-size: 0.78rem;
}

.quick-nav-id {
  font-size: 0.67rem;
  color: #596781;
}
</style>
