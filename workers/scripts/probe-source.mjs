import {readFile,writeFile} from 'node:fs/promises';
const vars=Object.fromEntries((await readFile('.dev.vars','utf8')).trim().split('\n').map(line=>line.split('=')));
const headers={Authorization:`Bearer ${vars.FANQIE_API_TOKEN}`,'X-MangaDock-Instance-ID':vars.FANQIE_INSTANCE_ID,'X-MangaDock-Version':'5.0','Content-Type':'application/json'};
async function call(path,init={}){const response=await fetch(`https://fanqie.dddinmx.cn${path}`,{...init,headers,signal:AbortSignal.timeout(30000)});if(!response.ok)throw new Error(`来源 HTTP ${response.status}`);const payload=await response.json();if(!payload.ok)throw new Error('来源格式无效');return payload.data;}
const search=await call('/v1/novels/search?q='+encodeURIComponent('三国演义')+'&page=1');
const book=search.items?.find(book=>book.title==='三国演义')||search.items?.[0];
if(!book)throw new Error('没有真实来源搜索结果');
const resolved=await call('/v1/resources/resolve',{method:'POST',body:JSON.stringify({target:String(book.book_id),kind:'novel'})});
const chapter=resolved.chapters?.find(ch=>/第一回|第1章|第1回/.test(ch.title))||resolved.chapters?.[2]||resolved.chapters?.[0];
console.log(JSON.stringify({book:{book_id:book.book_id,title:book.title,author:book.author},chapter,chapters:resolved.chapters?.length}));
await writeFile('.wrangler/source-probe.json',JSON.stringify({book,chapter,chapters:resolved.chapters?.length}));
