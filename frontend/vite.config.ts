import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The page calls /api/search. In development that is proxied to scripts/dev_api.py, which serves
// the same file Vercel runs (frontend/api/search.py), so the path is identical in both places.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    strictPort: true,
    proxy: {
      '/api': 'http://127.0.0.1:8790',
    },
  },
})
