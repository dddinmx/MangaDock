const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

async function testTaskPolling() {
    const template = fs.readFileSync('templates/tasks.html', 'utf8');
    const source = template.slice(template.indexOf('    const csrfToken ='), template.lastIndexOf('</script>')).replace(/\{\{.*?\}\}/g, '1');
    const listeners = {}, timers = new Map();
    let writes = 0, fetches = 0, release;
    const current = {value: 'running', get innerHTML(){ return this.value; }, set innerHTML(v){writes++;this.value=v;}, contains: e => e === 'focused'};
    let next = {innerHTML: 'running', dataset: {active: 'true', deletableCount: '0', activeCount: '1', count: '1'}};
    const doc = {hidden: false, activeElement: null, addEventListener: (type, fn) => listeners[type] = fn,
        getElementById: id => id === 'task-records' ? current : null};
    const context = vm.createContext({document: doc, console,
        DOMParser: class {parseFromString(){return {getElementById:()=>next};}},
        fetch: async()=>{fetches++;await new Promise(r=>release=r);return {ok:true,text:async()=>''};},
        window: {location:{href:'http://fixture/tasks',reload(){throw Error('Automatic polling must not reload the page');}},
            setInterval(fn){timers.set(1,fn);return 1;}, clearInterval(id){timers.delete(id);}, addEventListener:(type,fn)=>listeners[type]=fn}});
    vm.runInContext(source, context);
    listeners.DOMContentLoaded(); assert.equal(timers.size, 1);
    const first = vm.runInContext('refreshTaskRecords()', context);
    await vm.runInContext('refreshTaskRecords()', context);
    assert.equal(fetches, 1, 'Only one refresh may be in flight');
    release(); await first; assert.equal(writes, 0, 'Unchanged records keep their DOM');
    next.innerHTML = 'progress 80';
    doc.activeElement = 'focused';
    const focused = vm.runInContext('refreshTaskRecords()', context); release(); await focused;
    assert.equal(writes, 0, 'A focused task control is not replaced');
    doc.activeElement = null;
    const changed = vm.runInContext('refreshTaskRecords()', context); release(); await changed;
    assert.equal(writes, 1); assert.equal(current.value, 'progress 80');
    doc.hidden = true; listeners.visibilitychange(); assert.equal(timers.size, 0);
    await vm.runInContext('refreshTaskRecords()', context); assert.equal(fetches, 3);
    doc.hidden = false;
    const leaving = vm.runInContext('refreshTaskRecords()', context);
    next.innerHTML = 'progress 90'; listeners.pagehide(); release(); await leaving;
    assert.equal(writes, 1, 'A response arriving after pagehide must not change the cached page');
    listeners.pageshow({persisted:false}); assert.equal(timers.size, 1, 'Returning resumes a single timer');
    next.dataset.active = 'false'; next.innerHTML = 'completed';
    const terminal = vm.runInContext('refreshTaskRecords()', context); release(); await terminal;
    assert.equal(writes, 2); assert.equal(timers.size, 0, 'Terminal task status stops polling');
    next.dataset.active = 'true'; next.innerHTML = 'new active task';
    listeners.pageshow({persisted:true}); release(); await new Promise(r=>setImmediate(r));
    assert.equal(writes,3); assert.equal(timers.size,1,'A cached return refreshes records and resumes newly active tasks');
}

function testLogUpdates() {
    const template = fs.readFileSync('templates/progress.html', 'utf8');
    const source = template.slice(template.indexOf('    let renderedLogEntries'),template.indexOf('    // 添加日志条目'));
    let replacements = 0;
    const container = {children:[],scrollHeight:800,clientHeight:200,scrollTop:0,
        replaceChildren(){replacements++;this.children=[];},
        appendChild(fragment){this.children.push(...fragment.children);}};
    const ctx = vm.createContext({document:{getElementById:()=>container,
        createElement:()=>({}),createDocumentFragment:()=>({children:[],appendChild(line){this.children.push(line);}})}});
    vm.runInContext(source,ctx);
    const update = entries => {ctx.entries=entries;vm.runInContext('updateTaskLogs(entries)',ctx);};
    update(['one','two']); const original=container.children[0];
    assert.equal(container.scrollTop,800);
    update(['one','two']); assert.equal(replacements,1,'Identical logs do no DOM work');
    container.scrollTop=100;update(['one','two','three']);
    assert.equal(container.children[0],original,'Existing log entries survive append');
    assert.equal(container.children.length,3);assert.equal(container.scrollTop,100,'Reading older logs keeps its position');
    container.scrollTop=590;update(['one','two','three','four']);assert.equal(container.scrollTop,800,'Near the end follows new lines');
    update(['reset']);assert.equal(replacements,2);assert.equal(container.children.length,1);
    update([]);assert.equal(container.children[0].className,'md-log-empty');
    update(['started']);assert.equal(container.children.length,1);assert.equal(container.children[0].textContent,'started');
}
function testNovelSearch() {
    const template = fs.readFileSync('templates/novel_detail.html','utf8');
    const source = template.slice(template.indexOf('    let novelChapterSearchIndex'),template.indexOf('    function sortNovelChapters'));
    let reads=0,writes=0;
    const rows=Array.from({length:1000},(_,i)=>({querySelector(){reads++;return {textContent:`第 ${i+1} 章`};},style:new Proxy({display:''},{set(t,k,v){writes++;t[k]=v;return true;}})}));
    const input={value:'第 1000'};
    const ctx=vm.createContext({document:{getElementById:()=>input,querySelectorAll:()=>rows}});
    vm.runInContext(source,ctx);vm.runInContext('searchNovelChapters()',ctx);assert.equal(rows[999].style.display,'flex');assert.equal(rows[0].style.display,'none');
    const prior=writes;vm.runInContext('searchNovelChapters()',ctx);assert.equal(writes,prior);assert.equal(reads,1000);
    input.value='';vm.runInContext('searchNovelChapters()',ctx);assert(rows.every(r=>r.style.display==='flex'));
}
async function testWebdavPolling() {
    const source=fs.readFileSync('templates/settings_webdav.html','utf8').match(/<script>([\s\S]*?)<\/script>/)[1].replace(/\{\{.*?\}\}/g,'/fixture');
    const listeners={},timers=new Map(),nodes=new Map();let fetches=0,release;
    const node=id=>{if(!nodes.has(id))nodes.set(id,{textContent:'',value:'',hidden:false});return nodes.get(id);};
    const doc={hidden:false,getElementById:node,querySelector:()=>node('button'),addEventListener:(type,fn)=>listeners[type]=fn};
    const context=vm.createContext({document:doc,
        window:{addEventListener:(type,fn)=>listeners[type]=fn},
        setTimeout(fn){timers.set(1,fn);return 1;},clearTimeout(id){timers.delete(id);},
        fetch:async()=>{fetches++;await new Promise(r=>release=r);return {ok:true,json:async()=>({status:'running',progress:{total:10,done:2},metadata:{}})};}});
    vm.runInContext(source,context);assert.equal(fetches,1);
    listeners.pageshow();assert.equal(fetches,1,'Initial pageshow cannot duplicate a pending request');
    listeners.pagehide();release();await new Promise(r=>setImmediate(r));
    assert.equal(node('webdav-sync-progress').textContent,'');assert.equal(timers.size,0,'A response after leaving does not repaint or reschedule');
    listeners.pageshow();assert.equal(fetches,2);release();await new Promise(r=>setImmediate(r));
    assert.equal(timers.size,1);assert(node('webdav-sync-progress').textContent.includes('2/10'));
    doc.hidden=true;listeners.visibilitychange();assert.equal(timers.size,0);assert.equal(fetches,2);
    doc.hidden=false;listeners.visibilitychange();assert.equal(fetches,3);
    listeners.visibilitychange();assert.equal(fetches,3,'Visibility events do not overlap requests');
    release();await new Promise(r=>setImmediate(r));assert.equal(timers.size,1);
}
(async()=>{await testTaskPolling();await testWebdavPolling();testLogUpdates();testNovelSearch();console.log('Task polling preserves focus and page lifecycle; logs retain scroll position; novel search reuses 1000 titles');})().catch(e=>{console.error(e);process.exitCode=1;});
