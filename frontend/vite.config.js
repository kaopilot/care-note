import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Backend runs on 8000; proxying keeps the frontend origin-agnostic.
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: true, rewrite: p => p.replace(/^\/api/, '') },
    },
  },
  // Component tests. jsdom rather than a real browser: what these cover is
  // offset arithmetic and conditional rendering, neither of which needs a
  // compositor. The one thing jsdom genuinely cannot do — lay text out — is
  // handled by building Ranges explicitly rather than by simulating a drag.
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.js'],
    include: ['src/**/*.test.{js,jsx}'],
  },
})
