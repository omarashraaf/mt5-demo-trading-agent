import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

const isElectronBuild = process.env.ELECTRON_BUILD === '1';

export default defineConfig({
  base: isElectronBuild ? './' : '/',
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 5173,
  },
});
