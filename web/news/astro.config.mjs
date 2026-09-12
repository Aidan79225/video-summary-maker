// @ts-check
import { defineConfig } from 'astro/config';
import node from '@astrojs/node';
import tailwindcss from '@tailwindcss/vite';

// SSR：內容每天增加，靜態重建整站在 Raspberry Pi 上太慢。
export default defineConfig({
  output: 'server',
  adapter: node({ mode: 'standalone' }),
  server: { host: true },
  vite: {
    plugins: [tailwindcss()],
  },
});
