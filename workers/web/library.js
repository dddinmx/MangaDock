import {api,me,mutation} from './api.js';
const error=document.querySelector('#error');
function report(problem){error.textContent=problem.message;error.hidden=false;}
const element=(tag,text,cls)=>{const node=document.createElement(tag);node.textContent=text;if(cls)node.className=cls;return node;};
let user,activeJobs=false;
async function refresh(){
 const [books,jobs]=await Promise.all([api('/api/books'),api('/api/jobs')]);
 activeJobs=jobs.some(job=>['queued','submitting','running'].includes(job.state)); const library=document.querySelector('#library');library.replaceChildren();
 for(const book of books){const card=element('article','','book'),link=element('a','继续阅读','read-link');link.href=`/reader?book=${encodeURIComponent(book.id)}`;card.append(element('div','棧','book-art'),element('h3',book.title),element('small',book.author),link);library.append(card);}
 if(!books.length)library.append(element('p','书架还没有小说，下载后会显示在这里。'));
 document.querySelector('#jobs').replaceChildren(...jobs.map(job=>element('p',`${job.source_id} · ${job.state} · ${job.message}`)));
}
async function download(id){await api('/api/jobs',mutation(user.csrf_token,{book_id:id}));await refresh();}
document.querySelector('#search-form').addEventListener('submit',async e=>{e.preventDefault();try{const data=await api(`/api/search?q=${encodeURIComponent(e.target.q.value)}`),results=document.querySelector('#search-results');results.replaceChildren();for(const book of data.items||[]){const row=element('div','','search-item'),button=element('button','下载');button.addEventListener('click',()=>download(String(book.book_id)).catch(report));row.append(element('span',`${book.title} · ${book.author||'未知作者'}`),button);results.append(row);}}catch(problem){report(problem);}});
document.querySelector('#download-form').addEventListener('submit',async e=>{e.preventDefault();const button=e.target.querySelector('button');button.disabled=true;try{await download(e.target.book_id.value);}catch(problem){report(problem);}finally{button.disabled=false;}});
document.querySelector('#logout').addEventListener('click',async()=>{try{await api('/api/v1/auth/logout',mutation(user.csrf_token,{}));location.assign('/login');}catch(problem){report(problem);}});
try{user=await me();if(user.role!=='admin')document.querySelector('#download-form').hidden=true;await refresh();setInterval(()=>{if(!document.hidden&&activeJobs)refresh().catch(report);},5000);}catch(problem){report(problem);}
