import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [vue()],
  // Build output goes to frontend/dist/, which the Python backend
  // serves as static files (see retiboard/api/__init__.py).
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
  server: {
    // Vite dev server — only used during frontend development.
    // Production always uses the FastAPI static mount.
    port: 5173,
    strictPort: true,
    // Proxy API calls and WebSocket to the Python backend during dev.
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8787',
        changeOrigin: false,
      },
      '/ws': {
        target: 'ws://127.0.0.1:8787',
        ws: true,
      },
    },
  },
})
