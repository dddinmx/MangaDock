import {api,me} from './api.js';
const params=new URLSearchParams(location.search),id=params.get('book');
function xml(value){const doc=new DOMParser().parseFromString(value,'application/xml');if(doc.querySelector('parsererror'))throw new Error('EPUB 元数据损坏');return doc;}
function entryName(base,href){const url=new URL(href,`https://epub.invalid/${base}`);if(url.origin!=='https://epub.invalid')throw new Error('EPUB 外部引用不支持');return decodeURIComponent(url.pathname.slice(1));}
async function start(){
 if(!/^[a-zA-Z0-9_-]{1,64}$/.test(id||''))throw new Error('无效书籍 ID');
 const limit=33554432;
 const [user,books,progress,head]=await Promise.all([me(),api('/api/books'),api(`/api/books/${id}/progress`),fetch(`/api/books/${id}/file`,{method:'HEAD'})]);
 if(!head.ok)throw new Error('EPUB 下载失败');
 const book=books.find(book=>book.id===id);if(!book)throw new Error('作品不存在');
 const advertised=Number(head.headers.get('Content-Length'));
 if(!Number.isSafeInteger(advertised)||advertised<1||advertised>limit||Number(book.size)>limit)throw new Error('首阶段阅读器仅支持 32 MB 以内的 EPUB');
 const response=await fetch(`/api/books/${id}/file`);
 if(!response.ok){await response.body?.cancel();throw new Error('EPUB 下载失败');}
 const received=Number(response.headers.get('Content-Length')||advertised);
 if(!Number.isSafeInteger(received)||received<1||received>limit){await response.body?.cancel();throw new Error('首阶段阅读器仅支持 32 MB 以内的 EPUB');}
 const zip=await JSZip.loadAsync(await response.arrayBuffer());
 const container=zip.file('META-INF/container.xml');if(!container)throw new Error('缺少 EPUB container');
 const path=xml(await container.async('string')).getElementsByTagNameNS('*','rootfile')[0]?.getAttribute('full-path');
 const packageFile=zip.file(path||'');if(!packageFile)throw new Error('缺少 EPUB package');
 const opf=xml(await packageFile.async('string')),items=new Map([...opf.getElementsByTagNameNS('*','item')].map(node=>[node.getAttribute('id'),node]));
 const chapters=[...opf.getElementsByTagNameNS('*','itemref')].filter(node=>node.getAttribute('linear')!=='no').map(node=>items.get(node.getAttribute('idref'))).filter(node=>node?.getAttribute('media-type')==='application/xhtml+xml').map(node=>entryName(path,node.getAttribute('href')));
 if(!chapters.length||chapters.length>10000)throw new Error('EPUB 章节数量无效');
 // Do not inject EPUB HTML, scripts, CSS or external images into the application origin.
 async function getChapter(index){
  if(index<0||index>=chapters.length)throw new Error('章节不存在');
  const entry=zip.file(chapters[index]);if(!entry)throw new Error('EPUB 章节文件缺失');
  if(entry._data?.uncompressedSize>2097152)throw new Error('单章超过 2 MB 限制');
  const doc=xml(await entry.async('string'));
  for(const node of [...doc.getElementsByTagNameNS('*','script'),...doc.getElementsByTagNameNS('*','style')])node.remove();
  const heading=[...doc.getElementsByTagNameNS('*','h1'),...doc.getElementsByTagNameNS('*','h2')][0];
  let paragraphs=[...doc.getElementsByTagNameNS('*','p')].map(p=>p.textContent.trim()).filter(Boolean);
  if(!paragraphs.length)paragraphs=[...doc.getElementsByTagNameNS('*','body')].flatMap(node=>node.textContent.split(/\n+/).map(v=>v.trim()).filter(Boolean));
  return {index,title:heading?.textContent.trim()||`第 ${index+1} 章`,paragraphs,total_chapters:chapters.length};
 }
 const requested=params.has('start_chapter')?Number(params.get('start_chapter')):progress?.chapter||0;
 const index=Math.max(0,Math.min(Number.isSafeInteger(requested)?requested:0,chapters.length-1));
 const badge=document.createElement('div');badge.style.cssText='position:fixed;right:12px;top:12px;z-index:50;color:#fda4af;font-size:12px';badge.setAttribute('role','status');document.body.append(badge);
 window.startReader({id,csrf:user.csrf_token,user:user.id,title:book.title,count:chapters.length,chapter:await getChapter(index),progress:progress?.chapter===index?progress:null,getChapter,saveStatus:ok=>{badge.textContent=ok?'':'进度同步失败，请检查网络';}});
 // The existing reader provides fonts, colors, horizontal swipe and vertical scrolling.
 const toc=document.querySelector('[aria-label="小说目录"]');
 toc.addEventListener('click',async event=>{event.preventDefault();const dialog=document.createElement('dialog');dialog.style.cssText='max-height:70vh;width:min(500px,85vw);background:#141419;color:white;border:1px solid #444;border-radius:12px';const close=document.createElement('button');close.textContent='关闭';close.onclick=()=>{dialog.close();dialog.remove();};dialog.append(close);for(let i=0;i<chapters.length;i++){const link=document.createElement('a');link.textContent=`第 ${i+1} 章`;link.href=`/reader?book=${id}&start_chapter=${i}`;link.style.cssText='display:block;padding:8px;color:inherit';dialog.append(link);}document.body.append(dialog);dialog.showModal();});
}
start().catch(problem=>{const message=document.createElement('p');message.textContent=problem.message;message.style.cssText='padding:100px 20px;color:#fda4af';document.body.prepend(message);});
