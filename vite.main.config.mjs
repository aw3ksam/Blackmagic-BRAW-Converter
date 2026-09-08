import { defineConfig } from 'vite';

// https://vitejs.dev/config
export default defineConfig({
  build: {
    rollupOptions: {
      external: [
        'electron',
        'child_process',
        'fs',
        'fs/promises',
        'path',
        'os',
        'events',
        'stream',
        'util',
        'url'
      ],
      output: {
        entryFileNames: 'main.js',
      },
    },
  },
});
