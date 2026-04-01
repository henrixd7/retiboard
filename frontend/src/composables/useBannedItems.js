import { computed, ref, watch } from 'vue'
import { apiJson } from '../utils/api.js'

function createEmptyBannedItems() {
  return {
    identities: [],
    attachments: [],
  }
}

function resolveBoardId(boardId) {
  return typeof boardId === 'function' ? boardId() : boardId
}

/**
 * Shared banned-list fetch and moderation actions for catalog/thread views.
 */
export function useBannedItems({
  boardId,
  isActive,
  moderation,
  cacheStore,
  moderationBusy,
  scheduleRefetch,
}) {
  const bannedItems = ref(createEmptyBannedItems())
  const bannedFileItems = computed(() => moderation.bannedFileHashList)

  async function withBusy(action) {
    moderationBusy.value = true
    try {
      await action()
    } finally {
      moderationBusy.value = false
    }
  }

  async function fetchBannedList() {
    try {
      bannedItems.value = await apiJson(`/api/boards/${resolveBoardId(boardId)}/control/banned`)
    } catch {}
  }

  async function unbanIdentityItem(item) {
    await withBusy(async () => {
      const targetId = item?.target_id
      if (!targetId) return
      await moderation.unbanIdentity(resolveBoardId(boardId), targetId)
      bannedItems.value.identities = bannedItems.value.identities
        .filter((entry) => entry.target_id !== targetId)
      scheduleRefetch?.(0)
    })
  }

  async function downgradeToHide(item) {
    await withBusy(async () => {
      const targetId = item?.target_id
      if (!targetId) return
      await moderation.unbanIdentity(resolveBoardId(boardId), targetId)
      await moderation.hideIdentity(resolveBoardId(boardId), targetId)
      bannedItems.value.identities = bannedItems.value.identities
        .filter((entry) => entry.target_id !== targetId)
      scheduleRefetch?.(0)
    })
  }

  async function unbanAttachmentItem(item) {
    await withBusy(async () => {
      const targetId = item?.target_id
      if (!targetId) return
      await moderation.unbanAttachment(resolveBoardId(boardId), targetId)
      bannedItems.value.attachments = bannedItems.value.attachments
        .filter((entry) => entry.target_id !== targetId)
      scheduleRefetch?.(0)
    })
  }

  async function unbanLocalFileHash(fileHash) {
    const normalized = String(fileHash || '').trim().toLowerCase()
    if (!normalized) return
    moderation.showBannedFilePlaceholder(normalized)
    moderation.unbanFileHash(normalized)
    cacheStore.removeByPrefix('attachments:')
  }

  if (isActive) {
    watch(isActive, (active) => {
      if (active) fetchBannedList()
    }, { immediate: true })
  }

  return {
    bannedItems,
    bannedFileItems,
    fetchBannedList,
    unbanIdentityItem,
    downgradeToHide,
    unbanAttachmentItem,
    unbanLocalFileHash,
  }
}
