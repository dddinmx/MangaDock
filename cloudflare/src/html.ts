// Minimal server-rendered pages. They load the existing static/ CSS (served by
// Workers Static Assets at the same /static/... URLs) so the look matches the
// Flask UI. Full Jinja template parity is remaining work (see docs).
export const esc = (s: unknown) =>
  String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]!);

export function layout(title: string, body: string, opts: { user?: string } = {}): Response {
  const nav = opts.user
    ? `<nav class="md-topbar" style="display:flex;gap:1rem;padding:.75rem 1rem;align-items:center">
         <a href="/"><img src="/static/logo-192.png" alt="" width="28" height="28"> MangaDock</a>
         <a href="/download">下载</a><span style="flex:1"></span><span>${esc(opts.user)}</span>
         <form method="post" action="/logout" style="display:inline"><button type="submit">退出</button></form></nav>`
    : '';
  return new Response(
    `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>${esc(title)} - MangaDock</title><link rel="icon" href="/static/favicon.png"><link rel="manifest" href="/static/manifest.json">
<link href="/static/css/app.generated.css" rel="stylesheet"><link href="/static/css/md-theme.css" rel="stylesheet">
<link href="/static/css/ui-polish.css" rel="stylesheet"></head><body class="md-body">${nav}<main style="max-width:1100px;margin:0 auto;padding:1rem">${body}</main></body></html>`,
    { headers: { 'Content-Type': 'text/html; charset=utf-8' } },
  );
}

export function loginPage(error = ''): Response {
  return layout(
    '登录',
    `<div style="max-width:360px;margin:12vh auto"><h1 style="font-size:1.6rem;margin-bottom:1rem">MangaDock 登录</h1>
     ${error ? `<p role="alert" style="color:#c00">${esc(error)}</p>` : ''}
     <form method="post" action="/login"><label>用户名<br><input name="username" autocomplete="username" required style="width:100%"></label><br><br>
     <label>密码<br><input name="password" type="password" autocomplete="current-password" required style="width:100%"></label><br><br>
     <button type="submit">登录</button></form></div>`,
  );
}

export function readerScript(comic: string, chapter: number, totalChapters: number, totalPages: number): string {
  // Same endpoints/payload as templates/reader.html: POST /save_progress, GET /get_progress/<name>.
  return `<script>(function(){
  var comic=${JSON.stringify(comic)},chapter=${chapter},tc=${totalChapters},tp=${totalPages},sid=Math.random().toString(36).slice(2),t0=Date.now();
  var imgs=[].slice.call(document.querySelectorAll('img.md-page'));
  function cur(){var y=window.innerHeight/2;for(var i=0;i<imgs.length;i++){var r=imgs[i].getBoundingClientRect();if(r.bottom>y)return i;}return imgs.length-1;}
  var timer=null;function save(){fetch('/save_progress',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'same-origin',
    body:JSON.stringify({comic_name:comic,chapter:chapter,page:cur(),scroll_position:Math.round(scrollY),total_chapters:tc,total_pages:tp,
    reading_time:Math.round((Date.now()-t0)/1000),reading_session_id:sid,client_progress_ms:Date.now()})});}
  addEventListener('scroll',function(){clearTimeout(timer);timer=setTimeout(save,800);},{passive:true});
  addEventListener('pagehide',save);
  fetch('/get_progress/'+encodeURIComponent(comic),{credentials:'same-origin'}).then(function(r){return r.json()}).then(function(p){
    if(p&&p.chapter===chapter&&p.page>0&&imgs[p.page]){imgs[p.page].scrollIntoView();}});
})();</script>`;
}
