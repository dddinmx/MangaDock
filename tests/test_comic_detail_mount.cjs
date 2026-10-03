const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const listeners = new Map(), frames = [];
const control = () => ({dataset: {}, listeners: [], classList: {remove() {}, contains: () => false},
    removeAttribute() {}, addEventListener(name, fn) { this.listeners.push({name, fn}); }});
const reader = control(), toggle = control(), form = control();
const description = {classList: {contains: () => false}, scrollHeight: 180, clientHeight: 90};
const context = {
    document: {
        addEventListener: (name, fn) => listeners.set(name, fn),
        getElementById: name => name === 'comic-detail-description' ? description : null,
        querySelector: selector => selector === '#comic-detail-description, #novel-detail-description' ? description
            : selector === '[data-comic-synopsis-toggle], [data-novel-synopsis-toggle]' ? toggle : null,
        querySelectorAll: selector => selector === '[data-reader-entry]' ? [reader]
            : selector === '.comic-detail form:not([data-api-v1])' ? [form] : [],
    },
    window: {addEventListener() {}, requestAnimationFrame: fn => frames.push(fn)},
    clearTimeout, setTimeout,
};
vm.runInNewContext(fs.readFileSync('static/js/comic-detail.js', 'utf8'), context);
context.initializeComicDetail();
context.initializeComicDetail();
assert.equal(reader.listeners.length, 1, 'Reader navigation binds once');
assert.equal(toggle.listeners.length, 1, 'Synopsis binds once');
assert.equal(form.listeners.length, 1, 'Submission feedback binds once');
context.refreshComicSynopsisLayout();
assert.equal(toggle.hidden, false, 'Overflow is measured after a partial page becomes visible');
description.scrollHeight = 70;
context.refreshComicSynopsisLayout();
assert.equal(toggle.hidden, true, 'Short descriptions do not show a redundant toggle');
console.log('Partial detail mount is idempotent; synopsis layout refreshes after reveal');
