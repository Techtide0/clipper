import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The dashboard talks to the FastAPI backend (port 8000) through this proxy.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', rewrite: (p) => p.replace(/^\/api/, '') },
      '/clips': 'http://127.0.0.1:8000',
    },
  },
})
