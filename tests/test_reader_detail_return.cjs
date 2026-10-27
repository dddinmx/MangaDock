const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/js/reader-detail-return.js', 'utf8');
const origin = 'https://mangadock.test';

function page(path, {storage = new Map(), referrer = '', length = 2, blocked = false} = {}) {
    const events = {}, timers = new Map();
    let backs = 0, timerId = 0;
    const context = {
        URL, Date, Set, AbortController,
        location: {href: origin + path, origin, pathname: path.split('?')[0]},
        history: {length, back: () => backs++},
        sessionStorage: {
            getItem: key => { if (blocked) throw Error('Blocked'); return storage.get(key); },
            setItem: (key, value) => { if (blocked) throw Error('Blocked'); storage.set(key, value); },
            removeItem: key => storage.delete(key),
        },
        window: {addEventListener: (type, fn) => events[type] = fn},
        document: {referrer, addEventListener: (type, fn) => events[type] = fn,
            querySelector: () => null, importNode: node => node},
        setTimeout: (fn, delay) => { timers.set(++timerId, {fn, delay}); return timerId; },
        clearTimeout: id => timers.delete(id),
    };
    vm.runInNewContext(source, context);
    return {context, events, timers, storage, backs: () => backs};
}
function enter(p, href, modifiers = {}) {
    p.events.click({target: {closest: () => ({href: origin + href, target: '', hasAttribute: () => false})},
        button: 0, ...modifiers});
}
async function run() {
    const storage = new Map();
    const detail = page('/comic/123?return_to=%2Fcomics', {storage});
    const target = '/reader/demo?start_chapter=2&return_to=%2Fcomics';
    enter(detail, target, {ctrlKey: true});
    assert.equal(storage.size, 0, 'Opening a new tab cannot arm a history return');
    enter(detail, target);
    const reader = page(target, {storage, referrer: detail.context.location.href});
    assert(reader.context.window.returnReaderToDetail('/comic/demo?return_to=%2Fcomics'),
        'Name-based reader returns to the original task-ID detail');
    assert.equal(reader.backs(), 1);
    assert(!reader.context.window.returnReaderToDetail('/comic/other?return_to=%2Fcomics'));
    assert(!reader.context.window.returnReaderToDetail('/comic/demo?return_to=%2F'));

    for (const options of [{referrer: origin + '/other'}, {length: 1}, {blocked: true}]) {
        enter(detail, target);
        const invalid = page(target, {storage, referrer: detail.context.location.href, ...options});
        assert(!invalid.context.window.returnReaderToDetail('/comic/demo?return_to=%2Fcomics'),
            'Unverified history and blocked storage use the ordinary detail link');
    }
    assert(!page(target).context.window.returnReaderToDetail('/comic/demo'), 'Direct entry has no history shortcut');
    enter(detail, target);
    const valid = page(target, {storage, referrer: detail.context.location.href});
    valid.events.pagehide(); // Native browser back also requests a progress refresh.
    const restored = page('/comic/123?return_to=%2Fcomics', {storage});
    let bound = 0, replacements = 0;
    const progress = {dataset: {detailProgress: 'summary'}, replaceChildren: () => replacements++};
    const chapter = {href: origin + target, classList: {toggle: (cls, enabled) => assert(enabled)}};
    const retained = {isConnected: true, querySelectorAll: selector => selector === '[data-detail-progress]' ? [progress] : [chapter]};
    restored.context.document.querySelector = () => retained;
    restored.context.window.initializeComicDetail = () => bound++;
    restored.context.fetch = async () => ({ok: true, redirected: false, text: async () => 'fresh'});
    restored.context.DOMParser = class { parseFromString() {
        return {querySelector: () => ({childNodes: ['updated summary']}), querySelectorAll: () => [chapter]};
    }};
    restored.events.pageshow({persisted: true});
    assert.equal(replacements, 0, 'Progress refresh does not block the restored frame');
    const refresh = [...restored.timers.values()].find(timer => timer.delay === 250);
    await refresh.fn();
    assert.equal(replacements, 1);
    assert.equal(bound, 1);
    assert.equal(storage.has('mangadock-reader-detail-refresh'), false, 'Refresh marker is consumed once');
    restored.events.pageshow({persisted: true});
    assert.equal(bound, 1);

    storage.set('mangadock-reader-detail-refresh', JSON.stringify({detail: origin + '/comic/demo', at: Date.now(),
        ui: {scroll: 914, sort: 'asc', filter: '第3话', expanded: true, home: {url: origin + '/', hero: 390}}}));
    const evicted = page('/comic/demo', {storage});
    const sort = {}, search = {};
    let sorted = 0, filtered = 0, expanded = 0, scroll = 0;
    evicted.context.document.querySelector = selector => selector === '#chapter-sort' ? sort
        : selector === '#chapter-search' ? search : {click: () => expanded++};
    Object.assign(evicted.context.window, {sortChapters: () => sorted++, searchChapters: () => filtered++,
        requestAnimationFrame: fn => fn(), scrollTo: (_, y) => scroll = y});
    evicted.events.pageshow({persisted: false});
    assert.equal(sort.value, 'asc'); assert.equal(search.value, '第3话');
    assert.equal(sorted, 1); assert.equal(filtered, 1); assert.equal(expanded, 1);
    assert.equal(scroll, 914);
    assert.equal(evicted.context.window.mdReaderHomePosition.hero, 390,
        'UI and home poster position also survive BFCache eviction');
    console.log('Reader returns: verified history, aliases, native back, fallback and retained progress refresh passed');
}
run().catch(error => { console.error(error); process.exitCode = 1; });
