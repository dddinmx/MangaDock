const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../templates/reader.html'), 'utf8');

function functionSource(name) {
    const start = source.indexOf(`function ${name}(`);
    assert.ok(start >= 0, `${name} exists`);
    const bodyStart = source.indexOf('{', start);
    let depth = 0;
    for (let index = bodyStart; index < source.length; index++) {
        if (source[index] === '{') depth++;
        if (source[index] === '}' && --depth === 0) return source.slice(start, index + 1);
    }
    throw new Error(`Unclosed function: ${name}`);
}

assert.match(source, /\.back-btn\.is-pressed/);
assert.match(source, /\.back-btn\.is-leaving/);
assert.match(source, /reader-back-ripple/);
assert.match(source, /back-btn-spinner/);
assert.equal((source.match(/class="back-btn"/g) || []).length, 2);

function button(href) {
    return {
        href,
        className: 'back-btn',
        classList: {
            items: new Set(),
            add(...names) { names.forEach((name) => this.items.add(name)); },
            remove(...names) { names.forEach((name) => this.items.delete(name)); },
            contains(name) { return this.items.has(name); },
        },
        attrs: { href, 'aria-busy': null },
        getAttribute(name) { return this.attrs[name] ?? null; },
        setAttribute(name, value) { this.attrs[name] = String(value); },
        removeAttribute(name) { this.attrs[name] = null; },
    };
}

const frames = [];
const timers = new Map();
let nextTimer = 1;
const location = { href: '/reader' };
const mobile = button('/comic/1?return_to=/comics');
const desktop = button('/comic/1?return_to=/comics');
let flushed = 0;
let snapshots = 0;

const context = {
    readerBackLeaving: false,
    readerBackPressTimer: null,
    window: {
        location,
        requestAnimationFrame(callback) {
            frames.push(callback);
            return frames.length;
        },
        setTimeout(callback, delay) {
            const id = nextTimer++;
            timers.set(id, { callback, delay });
            return id;
        },
        clearTimeout(id) {
            timers.delete(id);
        },
    },
    document: {
        querySelectorAll(selector) {
            assert.equal(selector, '.back-btn');
            return [mobile, desktop];
        },
    },
    flushVisibleReadingTime() { flushed += 1; },
    submitProgressSnapshot() { snapshots += 1; },
};

const createHandlers = new Function('scope', `
    with (scope) {
        ${functionSource('readerBackButtons')}
        ${functionSource('clearReaderBackPress')}
        ${functionSource('pressReaderBackButton')}
        ${functionSource('resetReaderBackButtons')}
        ${functionSource('leaveReaderViaBackButton')}
        return {
            pressReaderBackButton,
            leaveReaderViaBackButton,
            resetReaderBackButtons,
            get leaving() { return readerBackLeaving; },
        };
    }
`);
const handlers = createHandlers(context);

handlers.pressReaderBackButton(mobile);
assert.equal(mobile.classList.contains('is-pressed'), true, 'pointer down shows press feedback');
assert.equal(handlers.leaveReaderViaBackButton(mobile), true);
assert.equal(handlers.leaving, true);
assert.equal(mobile.classList.contains('is-leaving'), true);
assert.equal(desktop.classList.contains('is-leaving'), true);
assert.equal(mobile.classList.contains('is-pressed'), false);
assert.equal(mobile.attrs['aria-busy'], 'true');
assert.equal(flushed, 1);
assert.equal(snapshots, 1);
assert.equal(location.href, '/reader', 'navigation waits for paint');
assert.equal(handlers.leaveReaderViaBackButton(desktop), false, 'second tap is ignored');
assert.equal(flushed, 1);

frames[0]();
frames[1]();
assert.equal(location.href, '/comic/1?return_to=/comics');

handlers.resetReaderBackButtons();
assert.equal(handlers.leaving, false);
assert.equal(mobile.classList.contains('is-leaving'), false);
assert.equal(mobile.attrs['aria-busy'], null);

console.log('Reader back button shows press then leaving feedback before navigating');
