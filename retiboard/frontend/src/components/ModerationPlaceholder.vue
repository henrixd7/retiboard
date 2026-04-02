<script setup>
/**
 * ModerationPlaceholder — shown wherever a post or thread is locally
 * suppressed (hidden, purged, blocked identity, or awaiting network sync).
 *
 * Props:
 *   kind           — 'post' | 'thread'
 *   reason         — why it's suppressed (drives label text and styling)
 *   primaryId      — short id shown in the stub (post_id or thread_id prefix)
 *   secondaryText  — optional subtitle (identity prefix, timestamp, status, etc.)
 *   canShow        — show "Show anyway" session exception button
 *   canHide        — show a persistent move-to-hidden action
 *   hideLabel      — text for the hide button
 *   canRestore     — show a persistent un-action button (unhide/unblock)
 *   restoreLabel   — text for the restore button
 *   canPurge       — show a purge button (for hidden items that aren't purged yet)
 *   canRedownload  — show an undo-purge / re-fetch button
 *   redownloadLabel — text for the re-download button (default: 'Re-fetch from network')
 *
 * Emits:
 *   show       — user wants a session-local exception (load anyway)
 *   hide       — user wants to move this item into the hidden bucket
 *   restore    — user wants to persistently undo the rule (unhide/unblock)
 *   purge      — user confirmed they want to hard-delete this item
 *   redownload — user wants to undo a purge / re-fetch content
 */
const props = defineProps({
  kind:            { type: String,  default: 'post' },
  reason:          { type: String,  default: '' },
  primaryId:       { type: String,  default: '' },
  secondaryText:   { type: String,  default: '' },
  showLabel:       { type: String,  default: 'Show anyway' },
  hideLabel:       { type: String,  default: 'Hide' },
  restoreLabel:    { type: String,  default: '' },
  canShow:         { type: Boolean, default: true },
  canHide:         { type: Boolean, default: false },
  canRestore:      { type: Boolean, default: false },
  canPurge:        { type: Boolean, default: false },
  canRedownload:   { type: Boolean, default: false },
  redownloadLabel: { type: String,  default: 'Re-fetch from network' },
})

const emit = defineEmits(['show', 'hide', 'restore', 'purge', 'redownload'])

const isPurged   = ['purged', 'purged_post', 'purged_thread'].includes(props.reason)
const isBanned   = props.reason === 'blocked_identity'
const isAwaiting = props.reason === 'awaiting_network'

function reasonLabel(reason) {
  switch (reason) {
    case 'hidden_thread':     return 'hidden thread'
    case 'hidden_post':       return 'hidden post'
    case 'hidden_identity':   return 'identity hidden'
    case 'blocked_identity':  return 'identity banned'
    case 'purged':            return 'purged locally'
    case 'purged_thread':     return 'thread purged locally'
    case 'purged_post':       return 'post purged locally'
    case 'awaiting_network':  return 'purge undone'
    default:                  return 'hidden'
  }
}
</script>

<template>
  <div class="mod-placeholder" :class="[kind, { 'is-purged': isPurged, 'is-banned': isBanned, 'is-awaiting': isAwaiting }]">
    <div class="mod-header">
      <span class="mod-badge" :class="{ purged: isPurged, banned: isBanned, awaiting: isAwaiting }">
        {{ isAwaiting ? 'Awaiting sync' : isPurged ? 'Purged · not shared' : isBanned ? 'Banned · not shared' : 'Not shared' }}
      </span>
      <span class="mod-reason">{{ reasonLabel(reason) }}</span>
    </div>

    <div class="mod-id">{{ kind }} {{ primaryId || 'unknown' }}</div>
    <div v-if="secondaryText" class="mod-secondary">{{ secondaryText }}</div>

    <div class="mod-actions">
      <!-- Session exception: load without changing the rule -->
      <button v-if="canShow && !isPurged && !isAwaiting" class="btn-show" @click="$emit('show')">
        {{ showLabel }}
      </button>

      <button v-if="canHide && !isAwaiting" class="btn-restore" @click="$emit('hide')">
        {{ hideLabel }}
      </button>

      <!-- Persistent un-action: unhide or unblock -->
      <button v-if="canRestore && !isAwaiting" class="btn-restore" @click="$emit('restore')">
        {{ restoreLabel }}
      </button>

      <!-- Purge: only shown on hidden items, not already-purged or awaiting -->
      <button v-if="canPurge && !isPurged && !isAwaiting" class="btn-purge" @click="$emit('purge')">
        Purge locally
      </button>

      <!-- Re-download / undo purge -->
      <button v-if="canRedownload && isPurged && !isAwaiting" class="btn-redownload" @click="$emit('redownload')">
        {{ redownloadLabel }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.mod-placeholder {
  background: #131328;
  border: 1px dashed #3b3b62;
  border-radius: 4px;
  padding: 0.65rem 0.8rem;
  color: #9090ad;
}
.mod-placeholder.is-purged {
  border-color: #5a3040;
  background: #160f14;
}
.mod-placeholder.is-awaiting {
  border-color: #3a4a30;
  background: #111a0e;
}

.mod-header {
  display: flex;
  align-items: center;
  gap: 0.45rem;
  margin-bottom: 0.3rem;
}

.mod-badge {
  font-size: 0.57rem;
  color: #c8c89a;
  background: #2b2b1b;
  border: 1px solid #5a5a32;
  padding: 0.07rem 0.25rem;
  border-radius: 999px;
  text-transform: uppercase;
  letter-spacing: 0.03em;
}
.mod-badge.purged {
  color: #c8a0a0;
  background: #2b1b1b;
  border-color: #7a4040;
}
.mod-placeholder.is-banned {
  border-color: #5a3050;
  background: #16101a;
}
.mod-badge.banned {
  color: #c8a0c8;
  background: #2b1b2b;
  border-color: #7a407a;
}
.mod-badge.awaiting {
  color: #a0c8a0;
  background: #1b2b1b;
  border-color: #407040;
}

.mod-reason {
  font-size: 0.73rem;
  color: #b0b0c8;
}

.mod-id {
  font-family: monospace;
  font-size: 0.7rem;
  color: #7d7d9c;
}

.mod-secondary {
  margin-top: 0.15rem;
  font-size: 0.7rem;
  color: #6a6a85;
}
.is-awaiting .mod-secondary {
  color: #607060;
  font-style: italic;
}

.mod-actions {
  display: flex;
  gap: 0.4rem;
  margin-top: 0.5rem;
  flex-wrap: wrap;
  align-items: center;
}

/* Show anyway — primary, visible */
.btn-show {
  font-family: inherit;
  font-size: 0.73rem;
  padding: 0.25rem 0.55rem;
  border-radius: 3px;
  cursor: pointer;
  border: 1px solid #4040a0;
  background: #2a2a5a;
  color: #c0c0ff;
}
.btn-show:hover { background: #383870; }

/* Restore (unhide/unblock) — secondary, quieter */
.btn-restore {
  font-family: inherit;
  font-size: 0.72rem;
  padding: 0.25rem 0.52rem;
  border-radius: 3px;
  cursor: pointer;
  border: 1px solid #384038;
  background: transparent;
  color: #80a880;
}
.btn-restore:hover { background: #1a281a; }

/* Purge — muted danger, clearly destructive but not alarming */
.btn-purge {
  font-family: inherit;
  font-size: 0.7rem;
  padding: 0.23rem 0.48rem;
  border-radius: 3px;
  cursor: pointer;
  border: 1px solid #5a3030;
  background: transparent;
  color: #b07070;
}
.btn-purge:hover { background: #2a1818; color: #d09090; }

/* Re-download / undo purge */
.btn-redownload {
  font-family: inherit;
  font-size: 0.71rem;
  padding: 0.25rem 0.52rem;
  border-radius: 3px;
  cursor: pointer;
  border: 1px solid #385838;
  background: transparent;
  color: #80b880;
}
.btn-redownload:hover { background: #1a2a1a; color: #a0d8a0; }
</style>
