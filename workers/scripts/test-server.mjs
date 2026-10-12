import {seed} from './seed-local.mjs';
import {spawn} from 'node:child_process';
await seed();
const child=spawn('npx',['wrangler','dev','--port','8787','--persist-to','.wrangler/state/e2e','--show-interactive-dev-session=false'],{stdio:'inherit',env:{...process.env,WRANGLER_SEND_METRICS:'false'}});
for(const signal of ['SIGINT','SIGTERM'])process.on(signal,()=>child.kill(signal));
child.on('exit',code=>process.exit(code??1));
