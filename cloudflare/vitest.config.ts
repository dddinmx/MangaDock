import { cloudflareTest } from '@cloudflare/vitest-plugin';
import { readD1Migrations } from '@cloudflare/vitest-plugin';
import { defineConfig } from 'vitest/config';
import path from 'node:path';

export default defineConfig(async () => {
  const migrations = await readD1Migrations(path.join(import.meta.dirname, 'migrations'));
  return {
    plugins: [
      cloudflareTest({
        wrangler: { configPath: './wrangler.jsonc' },
        miniflare: { bindings: { SESSION_SECRET: 'test-secret', TEST_MIGRATIONS: migrations, BAOZIMH_API_BASE: 'https://api.mock' } },
      }),
    ],
    test: { setupFiles: ['./test/apply-migrations.ts'] },
  };
});
