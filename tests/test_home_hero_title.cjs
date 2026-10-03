const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const frames = [], listeners = new Map();
let mobile = true, fontReady, reveal;
let layoutDirty = false, layoutFlushes = 0;
const readLayout = () => {
    if (layoutDirty) { layoutFlushes += 1; layoutDirty = false; }
};
function title(textWidth) {
    let size = '';
    const node = {style: {}, clientWidth: 350, baseSize: 32};
    Object.defineProperty(node.style, 'fontSize', {
        get: () => size,
        set(value) { size = value; layoutDirty = true; },
    });
    const text = {get scrollWidth() {
        readLayout();
        return Math.max(node.clientWidth, textWidth * (Number.parseFloat(node.style.fontSize) || node.baseSize) / 32);
    }};
    node.querySelector = () => text;
    return node;
}
const long = title(540), short = title(128);
const titles = [long, short, ...Array.from({length: 16}, () => title(580))];
const hero = {dataset: {}, clientWidth: 390, querySelectorAll: () => titles};
const context = {
    document: {querySelector: () => hero, fonts: {ready: {then(fn) {fontReady = fn;}}}},
    window: {
        matchMedia: () => ({matches: mobile}),
        getComputedStyle: node => ({fontSize: `${node.baseSize}px`}),
        addEventListener: (name, fn) => listeners.set(name, fn),
        requestAnimationFrame: fn => {frames.push(fn); return frames.length;},
    },
    ResizeObserver: class {constructor(fn) {reveal = fn;} observe() {}},
};
const source = fs.readFileSync('static/js/home-hero-title.js', 'utf8');
const runFrames = () => {while (frames.length) frames.shift()();};
vm.runInNewContext(source, context);
runFrames();
assert.equal(layoutFlushes, 1, 'Fitting 18 titles batches measurements into one layout flush');
assert.ok(Number.parseFloat(long.style.fontSize) < 32);
assert.ok(long.querySelector().scrollWidth <= long.clientWidth);
assert.equal(short.style.fontSize, '', 'Short titles retain their original size');

const initialSize = long.style.fontSize;
listeners.get('resize')();
listeners.get('resize')();
assert.equal(frames.length, 1, 'Resize events are coalesced');
runFrames();
assert.equal(long.style.fontSize, initialSize, 'Repeated fitting does not keep shrinking the font');

long.clientWidth = 600;
listeners.get('resize')(); runFrames();
assert.equal(long.style.fontSize, '', 'A wider screen restores the base size');
long.clientWidth = 0;
listeners.get('resize')(); runFrames();
assert.equal(long.style.fontSize, '');
long.clientWidth = 280;
reveal([{contentRect: {width: 320}}]); runFrames();
assert.ok(long.querySelector().scrollWidth <= 280, 'A restored homepage is fitted when it becomes visible');

long.baseSize = 40;
fontReady(); runFrames();
assert.ok(long.querySelector().scrollWidth <= 280, 'A loaded font is measured again');
mobile = false;
listeners.get('resize')(); runFrames();
assert.equal(long.style.fontSize, '', 'Desktop typography is restored');
vm.runInNewContext(source, context);
assert.equal(frames.length, 0, 'Mounting twice does not bind another initializer');
console.log('Hero titles fit once per layout change, restore their base size, and handle hidden views');
