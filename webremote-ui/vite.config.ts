import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { resolve } from 'path'

// Built assets land straight in integrations/webui/, which webremote.py serves.
// Served from the app itself at "/" by default; set AUDIOCRATE_BASE=/audiocrate/
// to build a bundle that can live under a subpath on rednoobmusic.com.
const base = process.env.AUDIOCRATE_BASE || '/'

export default defineConfig({
  base,
  plugins: [react()],
  build: {
    outDir: resolve(__dirname, '../integrations/webui'),
    emptyOutDir: true,
    // The phone may be on cellular through the tunnel — keep the payload small
    // and split the vendor chunk so app edits don't re-download Ionic.
    rollupOptions: {
      output: {
        manualChunks: {
          vendor: ['react', 'react-dom', 'react-router', 'react-router-dom'],
        },
      },
    },
  },
  server: {
    // `npm run dev` against a locally running AudioCrate
    proxy: {
      '/api': 'http://127.0.0.1:8770',
    },
  },
})
