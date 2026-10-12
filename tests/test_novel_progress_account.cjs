const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const template = fs.readFileSync('templates/novel_reader.html', 'utf8');
const start = template.indexOf('const browserProgressKey =');
const end = template.indexOf('const chapterApiBaseUrl =', start);
const source = template.slice(start, end);
const storage = new Map();
function savedPage(userId, serverPage = 0, serverClock = null) {
    const context = vm.createContext({
        readerUserId: userId, progressKey: 'novel:book', initialChapter: {index: 0},
        serverSavedPage: serverPage, serverProgressMs: serverClock,
        localStorage: {getItem: key => storage.get(key) ?? null},
    });
    return vm.runInContext(source + '\nsavedPage;', context);
}
storage.set('mangadock-novel-progress:novel:book', JSON.stringify({chapter: 0, page: 12, savedAt: 20000}));
assert.equal(savedPage(2), 0, 'An unscoped legacy cache cannot be attributed to the current user');
storage.set('mangadock-novel-progress:1:novel:book', JSON.stringify({chapter: 0, page: 12, savedAt: 20000}));
assert.equal(savedPage(1), 12, 'The owner resumes their local page');
assert.equal(savedPage(2), 0, 'Another account starts at its own page');
assert.equal(savedPage(1, 4, 30000), 4, 'Newer server progress takes precedence');
storage.set('mangadock-novel-progress:1:novel:book', '{bad json');
assert.equal(savedPage(1, 3), 3, 'Malformed local data falls back to server progress');
console.log('Novel progress account isolation passed.');
