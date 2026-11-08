import { fileURLToPath, URL } from 'node:url'
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      // Mirrors CloudStack's own alias, so the section file can be
      // copied into their ui/ tree unchanged.
      // CloudStack's own store, which only exists inside their build.
      // Aliased to a double so the shipped section file can import it
      // exactly as every other section does.
      '@/store': fileURLToPath(new URL('./tests/doubles/store.js', import.meta.url)),
      // CloudStack's shared view components, likewise.
      '@/components': fileURLToPath(new URL('./tests/doubles/components', import.meta.url)),
      // CloudStack's API helper, likewise.
      '@/api': fileURLToPath(new URL('./tests/doubles/api.js', import.meta.url)),
      '@': fileURLToPath(new URL('./src', import.meta.url))
    }
  },
  test: {
    environment: 'jsdom',
    include: ['tests/unit/**/*.spec.js'],
    globals: true
  }
})
