import {env, SELF, fetchMock, applyD1Migrations} from 'cloudflare:test';
import {beforeAll,beforeEach,afterEach,describe,it,expect} from 'vitest';
import {processJob,verifyPassword} from '../src/index';
import type {Env} from '../src/index';
const bindings=env as unknown as Env & {TEST_MIGRATIONS: Parameters<typeof applyD1Migrations>[1]};
const origin='https://mangadock.test',password='test-password-strong-2026';
let encoded:string;
beforeAll(async()=>{
 await applyD1Migrations(bindings.DB,bindings.TEST_MIGRATIONS);
 const key=await crypto.subtle.importKey('raw',new TextEncoder().encode(password),'PBKDF2',false,['deriveBits']);
 const bits=await crypto.subtle.deriveBits({name:'PBKDF2',salt:new TextEncoder().encode('test-salt'),iterations:100000,hash:'SHA-256'},key,256);
 encoded='pbkdf2:sha256:100000$test-salt$'+[...new Uint8Array(bits)].map(x=>x.toString(16).padStart(2,'0')).join('');
});
beforeEach(async()=>{fetchMock.activate();fetchMock.disableNetConnect();await bindings.DB.batch(['progress','jobs','books','sessions','login_attempts','users'].map(table=>bindings.DB.prepare(`DELETE FROM ${table}`))); await bindings.LIBRARY.delete((await bindings.LIBRARY.list()).objects.map(obj=>obj.key));await bindings.DB.prepare('INSERT INTO users(id,username,password_hash,role) VALUES (?,?,?,?)').bind('admin','admin',encoded,'admin').run();});
afterEach(()=>{fetchMock.assertNoPendingInterceptors();fetchMock.deactivate();});
async function login(username='admin',pwd=password){const response=await SELF.fetch(origin+'/api/v1/auth/login',{method:'POST',headers:{Origin:origin,'Content-Type':'application/json'},body:JSON.stringify({username,password:pwd})});return {response,cookie:response.headers.get('Set-Cookie')?.split(';')[0]||'',payload:await response.json() as any};}
const auth=(cookie:string,csrf:string)=>({Cookie:cookie,Origin:origin,'Content-Type':'application/json','X-CSRFToken':csrf});
const artifactBytes=new TextEncoder().encode('realistic-test-artifact-bytes');
async function completedJob(overrides:Record<string,unknown>={}){const digest=[...new Uint8Array(await crypto.subtle.digest('SHA-256',artifactBytes))].map(x=>x.toString(16).padStart(2,'0')).join('');return {id:'upstream-job',status:'completed',metadata:{title:'测试小说',author:'测试作者'},artifact:{url:'/v1/jobs/upstream-job/artifact',size:artifactBytes.length,sha256:digest,...overrides}};}
function mock(path:string,data:unknown,method='GET'){return fetchMock.get('https://fanqie.dddinmx.cn').intercept({path,method}).reply(200,JSON.stringify({ok:true,data}),{headers:{'content-type':'application/json'}});}
async function insertJob(state='queued',remote:string|null=null){await bindings.DB.prepare('INSERT INTO jobs(id,user_id,source_id,remote_id,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?)').bind('job','admin','123456789',remote,state,Math.floor(Date.now()/1000),Math.floor(Date.now()/1000)).run();}
describe('authentication and access',()=>{
 it('protects files, progress, jobs and library',async()=>{for(const path of ['/api/books','/api/books/123456789/file','/api/books/123456789/progress','/api/jobs'])expect((await SELF.fetch(origin+path)).status).toBe(401);});
 it('checks passwords, secure cookie, origin, CSRF and logout',async()=>{
  expect(await verifyPassword('wrong',encoded)).toBe(false);const {response,cookie,payload}=await login();expect(response.status).toBe(200);expect(response.headers.get('Set-Cookie')).toContain('HttpOnly; Secure; SameSite=Lax');
  expect((await SELF.fetch(origin+'/api/v1/auth/me',{headers:{Cookie:cookie}})).status).toBe(200);
  expect((await SELF.fetch(origin+'/api/jobs',{method:'POST',headers:{Cookie:cookie,Origin:origin,'Content-Type':'application/json'},body:'{}'})).status).toBe(403);
  expect((await SELF.fetch(origin+'/api/v1/auth/logout',{method:'POST',headers:{...auth(cookie,payload.data.csrf_token),Origin:'https://attacker.test'},body:'{}'})).status).toBe(403);
  expect((await SELF.fetch(origin+'/api/v1/auth/logout',{method:'POST',headers:auth(cookie,payload.data.csrf_token),body:'{}'})).status).toBe(200);
  expect((await SELF.fetch(origin+'/api/books',{headers:{Cookie:cookie}})).status).toBe(401);
 });
 it('rate limits in D1',async()=>{for(let i=0;i<5;i++)expect((await login('admin','wrong')).response.status).toBe(401);expect((await login()).response.status).toBe(429);});
 it('restricts download to admin',async()=>{await bindings.DB.prepare("INSERT INTO users VALUES ('reader','reader',?,'user')").bind(encoded).run();const {cookie,payload}=await login('reader');expect((await SELF.fetch(origin+'/api/jobs',{method:'POST',headers:auth(cookie,payload.data.csrf_token),body:JSON.stringify({book_id:'123456789'})})).status).toBe(403);});
});
describe('source → streaming R2 → reading → D1 progress',()=>{
 it('stores artifact, serves bytes/ranges and preserves newest per-user progress',async()=>{
  await insertJob();mock('/v1/jobs',{id:'upstream-job'},'POST');mock('/v1/jobs/upstream-job',await completedJob());fetchMock.get('https://fanqie.dddinmx.cn').intercept({path:'/v1/jobs/upstream-job/artifact',headers:{authorization:'Bearer test-token'}}).reply(200,artifactBytes,{headers:{'content-length':String(artifactBytes.length)}});
  await processJob(bindings,'job');expect((await bindings.DB.prepare('SELECT state FROM jobs WHERE id=?').bind('job').first<any>())?.state).toBe('completed');
  const {cookie,payload}=await login(),headers=auth(cookie,payload.data.csrf_token);
  const file=await SELF.fetch(origin+'/api/books/123456789/file',{headers:{Cookie:cookie}});expect(file.status).toBe(200);expect(new Uint8Array(await file.arrayBuffer())).toEqual(artifactBytes);
  const range=await SELF.fetch(origin+'/api/books/123456789/file',{headers:{Cookie:cookie,Range:'bytes=0-3'}});expect(range.status).toBe(206);expect(await range.text()).toBe('real');
  const ms=Date.now();for(const data of [{chapter:1,page:2,scroll:0,client_ms:ms},{chapter:0,page:0,scroll:0,client_ms:ms-10}])expect((await SELF.fetch(origin+'/api/books/123456789/progress',{method:'PUT',headers,body:JSON.stringify(data)})).status).toBe(200);
  const progress=await (await SELF.fetch(origin+'/api/books/123456789/progress',{headers:{Cookie:cookie}})).json() as any;expect(progress.data.chapter).toBe(1);expect(progress.data.page).toBe(2);
  await bindings.DB.prepare("INSERT INTO users VALUES ('other','other',?,'user')").bind(encoded).run();const other=await login('other');expect((await (await SELF.fetch(origin+'/api/books/123456789/progress',{headers:{Cookie:other.cookie}})).json() as any).data).toBeNull();
  expect((await SELF.fetch(origin+'/api/books/123456789/progress',{method:'PUT',headers,body:JSON.stringify({chapter:-1,page:0,scroll:0,client_ms:ms})})).status).toBe(400);
  await processJob(bindings,'job');expect((await bindings.DB.prepare('SELECT count(*) AS n FROM books').first<any>())?.n).toBe(1);
 });
 it('rejects checksum mismatch before publication',async()=>{await insertJob('running','upstream-job');mock('/v1/jobs/upstream-job',await completedJob({sha256:'0'.repeat(64)}));fetchMock.get('https://fanqie.dddinmx.cn').intercept({path:'/v1/jobs/upstream-job/artifact'}).reply(200,artifactBytes);await processJob(bindings,'job');expect(await bindings.DB.prepare('SELECT id FROM books').first()).toBeNull();expect((await bindings.DB.prepare('SELECT state FROM jobs').first<any>())?.state).toBe('error');});
 it('rejects artifact URLs from other hosts',async()=>{await insertJob('running','upstream-job');mock('/v1/jobs/upstream-job',await completedJob({url:'https://attacker.test/token'}));await processJob(bindings,'job');expect((await bindings.DB.prepare('SELECT state FROM jobs').first<any>())?.state).toBe('error');});
 it('rejects oversize artifacts before transfer',async()=>{await insertJob('running','upstream-job');mock('/v1/jobs/upstream-job',await completedJob({size:33554433}));await processJob(bindings,'job');expect(await bindings.DB.prepare('SELECT id FROM books').first()).toBeNull();expect((await bindings.DB.prepare('SELECT state FROM jobs').first<any>())?.state).toBe('error');});
 it('enforces the library storage budget before transferring bytes',async()=>{await insertJob('running','upstream-job');mock('/v1/jobs/upstream-job',await completedJob());await processJob({...bindings,MAX_LIBRARY_BYTES:'10'},'job');expect(await bindings.DB.prepare('SELECT id FROM books').first()).toBeNull();expect((await bindings.DB.prepare('SELECT message FROM jobs').first<any>())?.message).toContain('免费预算上限');});
 it('keeps pending work while upstream generates EPUB',async()=>{await insertJob('running','upstream-job');mock('/v1/jobs/upstream-job',{id:'upstream-job',status:'running'});await processJob(bindings,'job');expect(await bindings.DB.prepare('SELECT state,lease_until,attempts FROM jobs').first()).toMatchObject({state:'running',lease_until:0,attempts:1});});
 it('does not retry uncertain upstream submissions',async()=>{await insertJob();fetchMock.get('https://fanqie.dddinmx.cn').intercept({path:'/v1/jobs',method:'POST'}).reply(503,'unavailable');await processJob(bindings,'job');await processJob(bindings,'job');expect((await bindings.DB.prepare('SELECT state FROM jobs').first<any>())?.state).toBe('error');});
});
