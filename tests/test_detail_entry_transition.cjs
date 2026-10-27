const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const template = fs.readFileSync(path.join(__dirname, '../templates/comic_detail.html'), 'utf8');
const headScript = template.match(/\{% block extra_head %\}\s*<script>([\s\S]*?)<\/script>/)[1];
const script = headScript;
const key = 'mangadock-comic-detail-entry';
const storage = new Map();
const classes = new Set();
const listeners = new Map();
const frames = [];
const pathName = '/comic/example';

const context = vm.createContext({
    sessionStorage: {
        getItem: (name) => storage.get(name) ?? null,
        setItem: (name, value) => storage.set(name, value),
        removeItem: (name) => storage.delete(name),
    },
    location: { pathname: pathName },
    document: { addEventListener: (name, listener) => listeners.set(name, listener),
        getElementById: () => null,
        documentElement: { classList: {
        add: (name) => classes.add(name),
        remove: (name) => classes.delete(name),
    } } },
    window: {
        addEventListener: (name, listener) => listeners.set(name, listener),
        requestAnimationFrame: (callback) => frames.push(callback),
    },
    matchMedia: (query) => ({ matches: query.includes('max-width') }),
    Date,
});

storage.set(key, JSON.stringify({ path: pathName, at: Date.now() }));
vm.runInContext(script, context);
assert.equal(classes.has('md-detail-entering'), true);
assert.equal(storage.has(key), false);

listeners.get('animationend')({ animationName: 'md-detail-content-enter' });
assert.equal(classes.has('md-detail-entering'), false, 'Temporary layer is cleared after entry');
storage.set(key, JSON.stringify({ path: pathName, at: Date.now() }));
listeners.get('pageshow')({ persisted: true });
while (frames.length) frames.shift()();

listeners.get('pagehide')();
assert.equal(classes.has('md-detail-entering'), false);
storage.set(key, JSON.stringify({ path: pathName, at: Date.now() }));
listeners.get('pageshow')({ persisted: true });
while (frames.length) frames.shift()();
assert.equal(classes.has('md-detail-entering'), true);
assert.equal(storage.has(key), false);

listeners.get('pagehide')();
storage.set(key, JSON.stringify({ path: pathName, at: Date.now() - 130000 }));
listeners.get('pageshow')({ persisted: true });
while (frames.length) frames.shift()();
assert.equal(classes.has('md-detail-entering'), false, 'Stale navigation does not replay');

context.matchMedia = () => ({matches: false});
storage.set(key, JSON.stringify({path: pathName, at: Date.now()}));
listeners.get('pageshow')({persisted: true});
while (frames.length) frames.shift()();
assert.equal(classes.has('md-detail-entering'), false, 'Desktop detail entry must not slide');

console.log('Detail entry animation replays after back-forward cache restore');
