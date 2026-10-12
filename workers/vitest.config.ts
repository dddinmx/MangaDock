import {pbkdf2Sync} from 'node:crypto';
import { defineWorkersConfig, readD1Migrations } from '@cloudflare/vitest-pool-workers/config';
export default defineWorkersConfig(async () => ({
 test: {include:['test/**/*.test.ts'],poolOptions:{workers:{isolatedStorage:false,wrangler:{configPath:'./wrangler.jsonc'},miniflare:{bindings:{TEST_LEGACY_HASH:'pbkdf2:sha256:260000$legacy-salt$'+pbkdf2Sync('test-password-strong-2026','legacy-salt',260000,32,'sha256').toString('hex'), TEST_STRONG_HASH:'pbkdf2:sha256:600000$strong-salt$'+pbkdf2Sync('test-password-strong-2026','strong-salt',600000,32,'sha256').toString('hex'), TEST_MIGRATIONS:await readD1Migrations('./migrations'), FANQIE_API_TOKEN:'test-token', FANQIE_INSTANCE_ID:'test-instance'}}}}}
}));
