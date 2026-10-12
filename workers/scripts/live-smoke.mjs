import {readFile,writeFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {performance} from 'node:perf_hooks';
const target=process.env.MANGADOCK_URL||(JSON.parse(await readFile('deployment.json','utf8'))).url;
const password=process.env.MANGADOCK_ADMIN_PASSWORD;
if(!password)throw new Error('MANGADOCK_ADMIN_PASSWORD is required');
const origin=new URL(target).origin,measurements=[];
let cookie='',csrf='';
async function request(path,options={}){const start=performance.now(),response=await fetch(origin+path,{...options,headers:{Cookie:cookie,Origin:origin,'Content-Type':'application/json','X-CSRFToken':csrf,...options.headers},signal:AbortSignal.timeout(30000)});measurements.push({path,status:response.status,wall_ms:Math.round(performance.now()-start)});if(!response.ok)throw new Error(`${path} HTTP ${response.status}`);return response;}
const login=await request('/api/v1/auth/login',{method:'POST',body:JSON.stringify({username:'admin',password})});cookie=login.headers.get('set-cookie')?.split(';')[0];csrf=(await login.json()).data.csrf_token;
await request('/api/v1/auth/me');
let bookId=process.env.SOURCE_BOOK_ID;
if(bookId){
 const known=(await (await request('/api/books')).json()).data.some(book=>book.id===bookId);
 if(!known){
 const result=await request('/api/jobs',{method:'POST',body:JSON.stringify({book_id:bookId,chapter_ids:process.env.SOURCE_CHAPTER_IDS?.split(',')||[]})});const jobId=(await result.json()).data.id;
 const deadline=Date.now()+600000;
 let completed=false;
 while(Date.now()<deadline){const jobs=(await (await request('/api/jobs')).json()).data,job=jobs.find(j=>j.id===jobId);if(job.state==='error')throw new Error(`Download failed: ${job.message}`);if(job.state==='completed'){completed=true;break;}await new Promise(resolve=>setTimeout(resolve,10000));}
 if(!completed)throw new Error('Download smoke timeout');
 }
 const books=(await (await request('/api/books')).json()).data,book=books.find(b=>b.id===bookId);if(!book)throw new Error('R2 book missing');
 const bytes=await (await request(`/api/books/${bookId}/file`)).arrayBuffer();if(bytes.byteLength!==book.size)throw new Error('Read size mismatch');
 const stamp=Date.now();
 await request(`/api/books/${bookId}/progress`,{method:'PUT',body:JSON.stringify({chapter:0,page:1,scroll:0,client_ms:stamp})});
 const progress=(await (await request(`/api/books/${bookId}/progress`)).json()).data;if(progress.page!==1)throw new Error('Progress did not persist');
 measurements.push({book_id:bookId,bytes:bytes.byteLength,sha256:createHash('sha256').update(new Uint8Array(bytes)).digest('hex'),progress_saved:true});
}
for(let i=0;i<20;i++) await request('/api/books');
const result={url:origin,real_source_download:Boolean(bookId),measurements,warning:'wall_ms is request elapsed time, not Workers CPU. Free CPU acceptance requires deployed Cloudflare CPU telemetry.'};
await writeFile(process.env.LIVE_RESULTS_PATH||'live-results.json',JSON.stringify(result,null,2));
console.log(JSON.stringify(result,null,2));
