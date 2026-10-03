const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('templates/base.html', 'utf8');
const bootstrap = source.match(/document\.documentElement\.dataset\.coverMotion =[\s\S]*?;/)[0];
const controls = source.slice(source.indexOf('            const coverMotionToggle ='), source.indexOf('            if (groupMenuBtn && groupMenuDropdown)'));
const saved = new Map(), attributes = new Map();
let click, switchOn = false, changes = 0;
const button = {
    setAttribute: (key, value) => attributes.set(key, value),
    querySelector: () => ({classList: {toggle: (_, on) => {switchOn = on;}}}),
    addEventListener: (_, fn) => {click = fn;},
};
const context = {
    document: {documentElement: {dataset: {}}, querySelector: () => button},
    localStorage: {getItem: key => saved.get(key) || null, setItem: (key, value) => saved.set(key, value)},
    window: {dispatchEvent: event => {assert.equal(event.type, 'md-cover-motion-change'); changes++;}},
    Event: class {constructor(type) {this.type = type;}}, console: {warn() {}},
};
const mount = () => {vm.runInNewContext(bootstrap, context); vm.runInNewContext(`(() => {${controls}})()`, context);};
mount();
assert.equal(attributes.get('aria-checked'), 'false');
assert.equal(switchOn, false, 'First visit defaults off');
click();
assert.equal(switchOn, true);
assert.equal(saved.get('mangadock-cover-motion'), 'on');
context.document.documentElement.dataset = {};
mount();
assert.equal(switchOn, true, 'Reload restores an explicit on preference');
click();
assert.equal(saved.get('mangadock-cover-motion'), 'off');
mount();
assert.equal(switchOn, false, 'Reload keeps off');
context.localStorage.setItem = () => {throw Error('Blocked storage');};
click();
assert.equal(switchOn, true, 'A blocked persistence API does not break the current switch');
assert.equal(changes, 3);
console.log('Cover motion defaults off, persists both states and updates the accessible switch');
