// Local-only proxy adapter: managed Cloud environments require their configured HTTP proxy.
// Production Worker still uses native Cloudflare fetch; no proxy or laptop server is deployed.
import {Miniflare} from 'miniflare';
import {build} from 'esbuild';
import {readFile} from 'node:fs/promises';
import {pbkdf2Sync} from 'node:crypto';
const vars=Object.fromEntries((await readFile('.dev.vars','utf8')).trim().split('\n').map(line=>line.split('=')));
const compiled=await build({entryPoints:['src/index.ts'],bundle:true,write:false,format:'esm',platform:'browser',target:'es2022',external:['cloudflare:*']});
const mf=new Miniflare({port:Number(process.env.LOCAL_PORT||8790),modules:true,script:compiled.outputFiles[0].text,compatibilityDate:'2025-10-01',bindings:{...vars,FANQIE_API_URL:'https://fanqie.dddinmx.cn',MAX_ARTIFACT_BYTES:'33554432',MAX_LIBRARY_BYTES:'256000000'},d1Databases:['DB'],r2Buckets:['LIBRARY'],durableObjects:{AUTH_SERVICE:{className:'PasswordVerifier',useSQLite:true}},queueProducers:{DOWNLOADS:'downloads'},queueConsumers:{downloads:{maxBatchSize:1,maxRetries:3}},serviceBindings:{ASSETS:async()=>new Response('Local API-only source test',{status:404})},outboundService:async request=>{
 if(new URL(request.url).origin!=='https://fanqie.dddinmx.cn')throw new Error('Unexpected local test destination');
 const response=await fetch(request.url,{method:request.method,headers:Object.fromEntries([...request.headers].filter(([name])=>!['host','content-length','accept-encoding'].includes(name.toLowerCase()))),body:['GET','HEAD'].includes(request.method)?undefined:await request.arrayBuffer(),redirect:'manual',signal:AbortSignal.timeout(30000)});
 return new Response(response.body,{status:response.status,headers:response.headers});
}});
const db=await mf.getD1Database('DB');
for(const file of ['0001_initial.sql','0002_storage_budget.sql']){const sql=await readFile('migrations/'+file,'utf8');for(const statement of sql.split(';').map(s=>s.trim()).filter(Boolean))await db.prepare(statement).run();}
const password='local-e2e-password-2026',salt='local-test-salt',encoded=`pbkdf2:sha256:600000$${salt}$${pbkdf2Sync(password,salt,600000,32,'sha256').toString('hex')}`;
await db.prepare("INSERT INTO users VALUES ('admin','admin',?,'admin')").bind(encoded).run();
console.log('Real-source local runtime ready at '+await mf.ready);
for(const signal of ['SIGINT','SIGTERM'])process.on(signal,async()=>{await mf.dispose();process.exit(0);});
await new Promise(()=>{});
