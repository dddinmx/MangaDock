const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const template = fs.readFileSync('templates/base.html', 'utf8');
const end = template.indexOf('            const installPromptMutedUntil');
const start = template.lastIndexOf("            document.addEventListener('click', (event) => {", end);
function scenario(kind, cachedReturn = false) {
    let handler, loading = false, prevented = false;
    const frames = [], navigations = [];
    const source = kind === 'back' ? '/comic/example' : '/';
    const target = kind === 'back' ? '/' : '/comic/example';
    const cls = kind === 'back' ? '.md-detail-back' : '.md-home-detail-link';
    const link = {href: `http://localhost${target}`, dataset: {}, target: '',
        hasAttribute: () => false, getAttribute: () => target,
        matches: selector => selector.split(',').map(s => s.trim()).includes(cls),
        classList: {add() {}}, closest: () => null};
    const context = {URL, Date, location: {pathname: source},
        sessionStorage: {setItem() {}},
        document: {addEventListener: (_, fn) => handler = fn, body: {classList: {add() {}}},
            documentElement: {dataset: {detailReturnPath: cachedReturn ? '/' : undefined}}},
        window: {location: {href: `http://localhost${source}`, origin: 'http://localhost',
            pathname: source, search: '', assign: url => navigations.push(url)},
            history: {length: 2, back: () => navigations.push('back')},
            requestAnimationFrame: fn => frames.push(fn)},
        pendingAppNavigation: false, beginAppNavigation: () => {
            loading = true; context.pendingAppNavigation = true;
        }};
    vm.runInNewContext(template.slice(start, end), context);
    const event = {target: {closest: () => link}, button: 0, preventDefault: () => prevented = true};
    handler(event);
    assert(loading && prevented);
    assert.equal(navigations.length, 0);
    frames.shift()();
    assert.equal(navigations.length, 0, 'One feedback frame paints before navigation');
    frames.shift()();
    assert.deepEqual(navigations, [cachedReturn ? 'back' : link.href]);
    handler(event);
    assert.equal(frames.length, 0, 'Repeated clicks cannot enqueue another navigation');
    context.pendingAppNavigation = false; loading = prevented = false;
    handler({...event, ctrlKey: true});
    assert(!loading && !prevented, 'New tab shortcuts keep native behavior');
}
scenario('detail'); scenario('back'); scenario('back', true);
console.log('Detail/back feedback paints before navigation; safe history return and repeated clicks checked');
