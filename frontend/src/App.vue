<script setup>
import SystemConsole from './components/SystemConsole.vue'
</script>

<template>
  <div class="rb-app">
    <router-view :key="$route.path" />
    <SystemConsole />
  </div>
</template>

<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'Courier New', 'Lucida Console', monospace;
  background: #0e0e1e;
  color: #c0c0d0;
  min-height: 100vh;
  -webkit-font-smoothing: antialiased;
}
a { color: #7070ff; text-decoration: none; }
.rb-app { min-height: 100vh; }
</style>
