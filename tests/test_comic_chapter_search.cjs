const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
let titleReads = 0, listReads = 0, writes = 0;
const item = title => ({
    querySelector() { titleReads++; return {textContent: title}; },
    style: new Proxy({display: ''}, {set(target, key, value) { writes++; target[key] = value; return true; }}),
});
const input = {value: ''};
let list = {querySelectorAll() { listReads++; return items; }};
let items = Array.from({length: 1000}, (_, n) => item(`第 ${n + 1} 话`));
const context = {
    document: {getElementById: id => id === 'chapter-search' ? input : list, addEventListener() {}},
    window: {addEventListener() {}},
};
vm.runInNewContext(fs.readFileSync('static/js/comic-detail.js', 'utf8'), context);
input.value = '第 1000'; context.searchChapters();
assert.equal(items[999].style.display, 'flex');
assert.equal(items[0].style.display, 'none');
const previousWrites = writes;
context.searchChapters();
assert.equal(listReads, 1); assert.equal(titleReads, 1000);
assert.equal(writes, previousWrites, 'Repeating a filter does not rewrite every row');
input.value = ''; context.searchChapters();
assert(items.every(node => node.style.display === 'flex'), 'Clearing search restores all chapters');
items = [item('新漫画第一话')]; list = {querySelectorAll() {listReads++; return items;}};
input.value = '第'; context.searchChapters();
assert.equal(items[0].style.display, 'flex', 'A newly mounted comic builds its own index');
assert.equal(listReads, 2);
console.log('1000-chapter filtering reuses titles, avoids redundant writes and resets for a new comic');
