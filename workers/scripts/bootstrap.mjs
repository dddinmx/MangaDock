import {writeFile,readFile} from 'node:fs/promises';
import {pbkdf2Sync,randomBytes,createHash,createHmac} from 'node:crypto';
import {spawnSync} from 'node:child_process';
process.on('uncaughtException',error=>{console.error('::error::'+String(error.message).replaceAll('%','%25').replaceAll('\n','%0A').replaceAll('\r','%0D'));process.exit(1);});
const target=process.argv[2]||'staging';
if(!['staging','production'].includes(target))throw new Error('只允许 staging 或 production');
const account=process.env.CLOUDFLARE_ACCOUNT_ID,token=process.env.CLOUDFLARE_API_TOKEN,password=process.env.MANGADOCK_ADMIN_PASSWORD;
if(!/^[a-f0-9]{32}$/i.test(account||'')||!token)throw new Error('请配置 CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN');
if(!password||password.length<16||password.length>512)throw new Error('请配置至少 16 字符的 MANGADOCK_ADMIN_PASSWORD Secret');
const prefix=`mangadock-novel-${target}`;
async function cf(path,method='GET',data){const response=await fetch(`https://api.cloudflare.com/client/v4/accounts/${account}${path}`,{method,headers:{Authorization:`Bearer ${token}`,'Content-Type':'application/json'},body:data?JSON.stringify(data):undefined,signal:AbortSignal.timeout(30000)});const payload=await response.json();if(!response.ok||!payload.success)throw new Error(`Cloudflare ${method} ${path.split('?')[0]} HTTP ${response.status}; error codes ${(payload.errors||[]).map(e=>e.code).join(',')}`);return payload.result;}
async function pages(path){const results=[];for(let page=1;page<=100;page++){const part=await cf(`${path}?page=${page}&per_page=100`);results.push(...part);if(part.length<100)return results;}throw new Error('资源列表超过分页上限');}
// These calls provision free-compatible resources only. They never subscribe to Workers Paid or change billing plans.
const databases=await pages('/d1/database');
const db=databases.find(d=>d.name===prefix)||await cf('/d1/database','POST',{name:prefix});
const bucketList=await cf('/r2/buckets');
if(!bucketList.buckets.some(b=>b.name===prefix))await cf('/r2/buckets','POST',{name:prefix});
const queues=await pages('/queues');
if(!queues.some(q=>q.queue_name===prefix))await cf('/queues','POST',{queue_name:prefix});
// Generated resource IDs are deployment artifacts, never secrets or checked-in account-specific config.
const config={name:target==='production'?'mangadock-cloud':'mangadock-novel-staging',main:'src/index.ts',compatibility_date:'2025-10-01',assets:{directory:'./public',binding:'ASSETS',html_handling:'none',run_worker_first:['/api/*','/reader','/','/login']},vars:{FANQIE_API_URL:'https://fanqie.dddinmx.cn',MAX_ARTIFACT_BYTES:'33554432',MAX_LIBRARY_BYTES:target==='production'?'8000000000':'256000000'},d1_databases:[{binding:'DB',database_name:prefix,database_id:db.uuid,migrations_dir:'migrations'}],r2_buckets:[{binding:'LIBRARY',bucket_name:prefix}],queues:{producers:[{binding:'DOWNLOADS',queue:prefix}],consumers:[{queue:prefix,max_batch_size:1,max_retries:3,max_concurrency:1}]},triggers:{crons:['*/5 * * * *']},observability:{enabled:true},durable_objects:{bindings:[{name:'AUTH_SERVICE',class_name:'PasswordVerifier'}]},migrations:[{tag:'auth-v1',new_sqlite_classes:['PasswordVerifier']}]};
const filename=`wrangler.${target}.generated.json`;await writeFile(filename,JSON.stringify(config,null,2));
const migrate=spawnSync('npx',['wrangler','d1','migrations','apply',prefix,'--remote','--config',filename],{stdio:'inherit',env:{...process.env,CI:'true'}});if(migrate.status!==0)throw new Error('D1 migrations 失败');
const salt=randomBytes(16).toString('hex'),encoded=`pbkdf2:sha256:600000$${salt}$${pbkdf2Sync(password,salt,600000,32,'sha256').toString('hex')}`;
await cf(`/d1/database/${db.uuid}/query`,'POST',{sql:"INSERT INTO users(id,username,password_hash,role) VALUES ('admin','admin',?,'admin') ON CONFLICT(username) DO NOTHING",params:[encoded]});
// Original service supports stable instance registration. No signing/decryption service is relocated from its existing server.
const instance=process.env.FANQIE_INSTANCE_ID||'mdcf-'+createHash('sha256').update(`${account}:${target}`).digest('hex').slice(0,32);
let sourceToken=process.env.FANQIE_API_TOKEN;
if(!sourceToken){const registration='fqr_'+createHmac('sha256',password).update(`mangadock-source:${instance}`).digest('base64url');const response=await fetch('https://fanqie.dddinmx.cn/v1/register',{method:'POST',headers:{'X-MangaDock-Instance-ID':instance,'X-MangaDock-Registration-Key':registration,'X-MangaDock-Version':'5.0','X-MangaDock-System':'Cloudflare/Workers'},signal:AbortSignal.timeout(25000)});if(!response.ok)throw new Error(`来源注册 HTTP ${response.status}；请配置 FANQIE_API_TOKEN/FANQIE_INSTANCE_ID Secrets`);const payload=await response.json();sourceToken=payload.data?.token;if(!payload.ok||!/^fqk_[A-Za-z0-9_-]{32,128}$/.test(sourceToken||''))throw new Error('来源注册响应无效');}
// Keep secrets on stdin. Do not write them into config files, command arguments, deployment summaries or artifacts.
const deploy=spawnSync('npx',['wrangler','deploy','--config',filename],{stdio:'inherit'});if(deploy.status!==0)throw new Error('Worker 部署失败');
const secrets=spawnSync('npx',['wrangler','secret','bulk','--config',filename],{input:JSON.stringify({FANQIE_API_TOKEN:sourceToken,FANQIE_INSTANCE_ID:instance}),stdio:['pipe','inherit','inherit']});if(secrets.status!==0)throw new Error('Worker Secret 配置失败');
const subdomain=await cf('/workers/subdomain');
const url=`https://${config.name}.${subdomain.subdomain}.workers.dev`;
await writeFile('deployment.json',JSON.stringify({url,environment:target,database_id:db.uuid,bucket:prefix,queue:prefix},null,2));
if(process.env.GITHUB_STEP_SUMMARY)await import('node:fs/promises').then(fs=>fs.appendFile(process.env.GITHUB_STEP_SUMMARY,`部署地址：${url}\n\n管理员用户名：admin；密码来自 MANGADOCK_ADMIN_PASSWORD Secret。\n`));
console.log(`部署地址：${url}`);
