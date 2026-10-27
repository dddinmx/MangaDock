const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.join(__dirname, '../templates/reader.html'), 'utf8');
const start = source.indexOf('            if (anilistEnabled) {');
const end = source.indexOf('            if (!progressIntervalId)', start);
assert.ok(start >= 0 && end > start);
let poll;
let requests = 0;
const window = {setInterval(fn, ms) {assert.equal(ms, 60000); poll = fn;}};
const document = {hidden: false};
const fetch = async () => {requests++; return {ok: true, json: async () => ({chapter_index: 66})};};
const run = new Function('window', 'document', 'fetch', 'assert', `
 const anilistEnabled=true, chapters=[1], csrfToken='test', comicName='test';
 let currentChapter=60, savedScrollPosition=500;
 async function loadChapter(){throw new Error('Periodic sync must not navigate');}
 ${source.slice(start, end)}
 return async function(){await window.poll(); assert.equal(currentChapter,60); assert.equal(savedScrollPosition,500);};
`);
window.poll = () => poll();
(async () => {
 const verify = run(window, document, fetch, assert);
 await verify();
 assert.equal(requests, 1);
 document.hidden = true;
 await verify();
 assert.equal(requests, 1);
 console.log('AniList polling syncs progress without changing the active chapter or scroll position');
})();
