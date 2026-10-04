import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// One file, no code splitting, no hashed names: the built console must be a
// single self-contained HTML document that works on an offline phone.
export default defineConfig({
  plugins: [react()],
  build: {
    target: 'es2020',
    cssCodeSplit: false,
    assetsInlineLimit: 100000000,
    rollupOptions: {
      output: {
        inlineDynamicImports: true,
        entryFileNames: 'app.js',
        assetFileNames: 'app.[ext]',
      },
    },
  },
});
