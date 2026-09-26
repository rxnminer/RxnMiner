import { defineConfig } from 'astro/config';

export default defineConfig({
  site: 'https://rxnminer.github.io',
  base: process.env.SITE_BASE || '/',
  output: 'static',
  trailingSlash: 'always',
});
