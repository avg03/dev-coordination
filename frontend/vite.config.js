import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', '')
  return {
    plugins: [react()],
    define: {
      'import.meta.env.PROJECT_ID': JSON.stringify(env.PROJECT_ID || ''),
    },
    server: {
      port: 8501,
      host: '0.0.0.0',
      proxy: env.BACKEND_URL ? {
        '/api': { target: env.BACKEND_URL, changeOrigin: true },
        '/health': { target: env.BACKEND_URL, changeOrigin: true },
      } : undefined,
    },
  }
})
