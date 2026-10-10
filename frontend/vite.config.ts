import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// Dev proxy targets the existing aiohttp server (web/server.py).
// WEB_PORT matches the bot's env (default 8765, see .env.example).
const webPort = Number(process.env.WEB_PORT ?? 8765)

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': `http://127.0.0.1:${webPort}`,
    },
  },
  test: {
    environment: 'jsdom',
  },
})
