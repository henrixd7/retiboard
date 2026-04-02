<script setup>
/**
 * Fullscreen attachment overlay.
 * Images: click image OR outside to close.
 * Video: click outside to stop and close (controls need click).
 */
import { ref, onMounted, onUnmounted } from 'vue'

const props = defineProps({
  src: String,
  mimeType: String,
})
const emit = defineEmits(['close'])

const isVideo = props.mimeType?.startsWith('video/')
const isAudio = props.mimeType?.startsWith('audio/')
const videoRef = ref(null)

function onBackdropClick(e) {
  if (e.target.classList.contains('overlay-backdrop')) close()
}

function onImageClick() {
  close()
}

function close() {
  if (videoRef.value) {
    videoRef.value.pause()
    videoRef.value.currentTime = 0
  }
  emit('close')
}

function onKeydown(e) {
  if (e.key === 'Escape') close()
}

onMounted(() => {
  document.addEventListener('keydown', onKeydown)
  if (videoRef.value) videoRef.value.play()
})
onUnmounted(() => document.removeEventListener('keydown', onKeydown))
</script>

<template>
  <div class="overlay-backdrop" @click="onBackdropClick">
    <div class="overlay-content">
      <button class="overlay-close" @click="close">✕</button>

      <video
        v-if="isVideo" ref="videoRef"
        :src="src" controls autoplay
        class="overlay-video" @click.stop
      />
      <audio
        v-else-if="isAudio"
        :src="src" controls autoplay
        class="overlay-audio" @click.stop
      />
      <img
        v-else :src="src"
        class="overlay-img" @click="onImageClick"
      />
    </div>
  </div>
</template>

<style scoped>
.overlay-backdrop {
  position: fixed; inset: 0; z-index: 1000;
  background: rgba(0,0,0,0.92);
  display: flex; align-items: center; justify-content: center;
  cursor: pointer;
}
.overlay-content {
  position: relative;
  max-width: 95vw; max-height: 95vh;
  display: flex; align-items: center; justify-content: center;
}
.overlay-close {
  position: absolute; top: -30px; right: -10px;
  background: none; border: none; color: #808090;
  font-size: 1.2rem; cursor: pointer; z-index: 1001;
}
.overlay-close:hover { color: #fff; }
.overlay-img {
  max-width: 95vw; max-height: 92vh;
  object-fit: contain; cursor: pointer;
  border-radius: 2px;
}
.overlay-video {
  max-width: 90vw; max-height: 85vh;
  cursor: default; border-radius: 2px; outline: none;
}
.overlay-audio { min-width: 300px; cursor: default; }
</style>
