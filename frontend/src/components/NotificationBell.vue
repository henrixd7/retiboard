<script setup>
/**
 * Notification bell — shows unread count and dropdown of reply notifications.
 * Clicking a notification navigates to the thread and scrolls to the post.
 */
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useNotificationStore } from '../stores/notificationStore.js'

const router = useRouter()
const notifStore = useNotificationStore()
const showDropdown = ref(false)

function toggle() {
  showDropdown.value = !showDropdown.value
}

function closeDropdown() {
  showDropdown.value = false
}

function goToNotification(notif) {
  notifStore.markRead(notif.id)
  showDropdown.value = false
  router.push({
    name: 'thread',
    params: { boardId: notif.boardId, threadId: notif.threadId },
    query: { scrollTo: notif.postId },
  })
}

function timeAgo(ts) {
  const s = Math.floor(Date.now() / 1000 - ts)
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}
</script>

<template>
  <div class="notif-bell" v-click-outside="closeDropdown">
    <button
      :class="['bell-btn', { 'has-unread': notifStore.unreadCount > 0 }]"
      @click="toggle"
      :title="`${notifStore.unreadCount} unread`"
    >
      <svg class="bell-svg" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M8 1.5C5.8 1.5 4 3.3 4 5.5v2.7L3 9.7v.8h10v-.8l-1-1.5V5.5C12 3.3 10.2 1.5 8 1.5z" fill="currentColor"/>
        <path d="M6.5 11.5c0 .8.7 1.5 1.5 1.5s1.5-.7 1.5-1.5" fill="currentColor"/>
      </svg>
      <span v-if="notifStore.unreadCount > 0" class="bell-badge">
        {{ notifStore.unreadCount > 9 ? '9+' : notifStore.unreadCount }}
      </span>
    </button>

    <div v-if="showDropdown" class="notif-dropdown">
      <div class="notif-header">
        <span>Notifications</span>
        <button
          v-if="notifStore.notifications.length > 0"
          class="notif-clear" @click="notifStore.markAllRead()"
        >Mark all read</button>
      </div>

      <div v-if="notifStore.notifications.length === 0" class="notif-empty">
        No notifications yet.
      </div>

      <div v-else class="notif-list">
        <div
          v-for="n in notifStore.notifications.slice(0, 20)" :key="n.id"
          :class="['notif-item', { unread: !n.read }]"
          @click="goToNotification(n)"
        >
          <div class="notif-text">
            <span class="notif-ref">&gt;&gt;{{ n.postId.substring(0, 10) }}</span>
            replied to your post
            <span class="notif-own">&gt;&gt;{{ n.refPostId.substring(0, 10) }}</span>
          </div>
          <div class="notif-meta">{{ timeAgo(n.timestamp) }}</div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.notif-bell {
  position: relative;
  display: inline-flex;
  align-items: center;
}
.bell-btn {
  background: none;
  border: none;
  cursor: pointer;
  position: relative;
  padding: 0;
  line-height: 1;
  color: #404058;
  transition: color 0.15s;
  display: flex;
  align-items: center;
}
.bell-btn:hover { color: #707090; }
.bell-btn.has-unread { color: #8080c0; }
.bell-svg {
  width: 25px;
  height: 25px;
  display: block;
}
.bell-badge {
  position: absolute;
  top: -2px;
  right: -3px;
  background: #8060c0;
  color: #e0e0ff;
  font-size: 0.5rem;
  font-weight: bold;
  min-width: 12px;
  height: 12px;
  border-radius: 6px;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0 2px;
  font-family: inherit;
}

.notif-dropdown {
  position: absolute;
  top: 100%;
  right: 0;
  width: 320px;
  max-height: 400px;
  overflow-y: auto;
  background: #14142e;
  border: 1px solid #3a3a6a;
  border-radius: 4px;
  box-shadow: 0 6px 24px rgba(0, 0, 0, 0.5);
  z-index: 1500;
}
.notif-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 0.5rem 0.7rem;
  border-bottom: 1px solid #2a2a4a;
  font-size: 0.78rem;
  color: #a0a0c0;
}
.notif-clear {
  background: none;
  border: none;
  color: #6060a0;
  font-family: inherit;
  font-size: 0.68rem;
  cursor: pointer;
}
.notif-clear:hover { color: #a0a0ff; }

.notif-empty {
  padding: 1.5rem 0.7rem;
  text-align: center;
  color: #505060;
  font-size: 0.78rem;
}

.notif-list {
  display: flex;
  flex-direction: column;
}
.notif-item {
  padding: 0.5rem 0.7rem;
  cursor: pointer;
  border-bottom: 1px solid #1e1e3a;
  transition: background 0.15s;
}
.notif-item:hover { background: #1e1e40; }
.notif-item.unread { background: #1a1a3a; border-left: 3px solid #6060c0; }

.notif-text {
  font-size: 0.75rem;
  color: #b0b0c0;
  line-height: 1.35;
}
.notif-ref { color: #7070ff; font-family: monospace; }
.notif-own { color: #80c080; font-family: monospace; }
.notif-meta {
  font-size: 0.65rem;
  color: #505060;
  margin-top: 0.15rem;
}
</style>
