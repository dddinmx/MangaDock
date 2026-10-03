const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');

// A selection hidden by a later search must still be cleared by “清空选择”.
const groupTemplate = fs.readFileSync(path.join(root, 'templates/comic_groups.html'), 'utf8');
const groupScript = groupTemplate.match(/\{% block extra_scripts %\}\s*<script>([\s\S]*?)<\/script>/)[1];
const items = ['alpha', 'beta'].map((name) => {
    const checkbox = { checked: false };
    return {
        dataset: { search: name },
        hidden: false,
        checkbox,
        querySelector: () => checkbox,
    };
});
const search = { value: 'alpha', addEventListener() {} };
const count = { textContent: '' };
const submit = { disabled: true };
const empty = { hidden: true };
const groupContext = vm.createContext({
    document: {
        querySelectorAll: () => items,
        getElementById(id) {
            return {
                'comic-group-search': search,
                'comic-selected-count': count,
                'comic-batch-submit': submit,
                'comic-group-empty': empty,
            }[id];
        },
    },
});
vm.runInContext(groupScript, groupContext);
vm.runInContext('filterComicItems(); selectVisibleComics()', groupContext);
assert.equal(items[0].checkbox.checked, true);
search.value = 'beta';
vm.runInContext('filterComicItems(); clearComicSelection()', groupContext);
assert.deepEqual(items.map((item) => item.checkbox.checked), [false, false]);
assert.equal(count.textContent, 0);
assert.equal(submit.disabled, true);

// Novel selection must also clear books hidden by a later filter.
const novelTemplate = fs.readFileSync(path.join(root, 'templates/novel_groups.html'), 'utf8');
const novelScript = novelTemplate.match(/\{% block extra_scripts %\}\s*<script>([\s\S]*?)<\/script>/)[1];
const novelContext = vm.createContext({document: {
    querySelectorAll: () => items,
    getElementById(id) { return {'novel-group-search': search, 'novel-selected-count': count, 'novel-batch-submit': submit, 'novel-group-empty': empty}[id]; }
}});
vm.runInContext(novelScript, novelContext);
search.value = 'alpha';
vm.runInContext('filterNovelItems(); selectVisibleNovels(true)', novelContext);
assert.equal(items[0].checkbox.checked, true);
search.value = 'beta';
vm.runInContext('filterNovelItems(); selectVisibleNovels(false)', novelContext);
assert.deepEqual(items.map(item => item.checkbox.checked), [false, false]);
assert.equal(count.textContent, 0);
assert.equal(submit.disabled, true);

// An old image retry must not overwrite a newer source or reload a successful image.
const coverScript = fs.readFileSync(path.join(root, 'static/js/cover-retry.js'), 'utf8');
const listeners = new Map();
const timers = new Map();
let nextTimer = 1;
const coverContext = vm.createContext({
    document: { addEventListener: (type, listener) => listeners.set(type, listener) },
    setTimeout(callback) {
        const id = nextTimer++;
        timers.set(id, callback);
        return id;
    },
    clearTimeout: (id) => timers.delete(id),
    Date,
    WeakMap,
});
vm.runInContext(coverScript, coverContext);
function image(src) {
    const attributes = new Map([['src', src], ['data-fallback', '/static/default-comic-cover.jpg']]);
    return {
        tagName: 'IMG',
        hasAttribute: (name) => attributes.has(name),
        getAttribute: (name) => attributes.get(name) ?? null,
        setAttribute: (name, value) => attributes.set(name, String(value)),
        removeAttribute: (name) => attributes.delete(name),
        get src() { return attributes.get('src'); },
        set src(value) { attributes.set('src', value); },
    };
}
function emit(type, img) { listeners.get(type)({ target: img }); }
function runTimers() {
    for (const [id, callback] of [...timers]) {
        timers.delete(id);
        callback();
    }
}

const changed = image('/static/cover/old.jpg');
emit('error', changed);
changed.src = '/static/cover/new.jpg';
emit('error', changed);
assert.equal(timers.size, 1);
runTimers();
assert.match(changed.src, /^\/static\/cover\/new\.jpg\?retry=/);

const switched = image('/static/cover/first.jpg');
emit('error', switched);
switched.src = '/static/cover/second.jpg';
runTimers();
assert.equal(switched.src, '/static/cover/second.jpg');

const recovered = image('/static/cover/recovered.jpg');
emit('error', recovered);
emit('load', recovered);
assert.equal(timers.size, 0);
runTimers();
assert.equal(recovered.src, '/static/cover/recovered.jpg');

console.log('Frontend selection and cover retry regressions pass');
