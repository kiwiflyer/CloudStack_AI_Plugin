// Licensed to the Apache Software Foundation (ASF) under one or more
// contributor license agreements. See the NOTICE file distributed with this
// work for additional information regarding copyright ownership. The ASF
// licenses this file to you under the Apache License, Version 2.0 (the
// "License"); you may not use this file except in compliance with the
// License. You may obtain a copy of the License at
//
//   http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
// WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
// License for the specific language governing permissions and limitations
// under the License.
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
