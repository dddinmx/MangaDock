const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../templates/reader.html'), 'utf8');
const start = source.indexOf('        function syncContinuousReading()');
const end = source.indexOf('        async function appendNextChapter()', start);
if (start < 0 || end < start) throw new Error('Continuous reader function not found');

const chapter = { index: 0, ready: true, hasLoadedPage: false,
    element: { getBoundingClientRect: () => ({ top: 0, bottom: 20000 }) } };
let appended = 0;
const run = new Function('chapter', 'onAppend', `
    const window = { innerHeight: 800, scrollY: 0 };
    const pageContent = { getBoundingClientRect: () => ({ top: 0 }) };
    const continuousSections = [chapter];
    const chapters = [{ format: 'cbz' }, { format: 'cbz' }];
    const currentChapter = 0, autoScrollChapterTransitionLocked = false;
    const savedScrollPosition = 0, isDraggingProgressSlider = true;
    function isDesktopViewport() { return false; }
    function appendNextChapter() { onAppend(); }
    ${source.slice(start, end)}
    syncContinuousReading();
`);

run(chapter, () => appended++);
assert.equal(appended, 0);
chapter.hasLoadedPage = true;
run(chapter, () => appended++);
assert.equal(appended, 1, 'next chapter should begin before the reader reaches its end');
console.log('Reader starts the next CBZ chapter after the first current image loads');

const queueStart = source.indexOf('        const cbzImageQueue');
const queueEnd = source.indexOf('        function prioritizeVisibleCbzImages()', queueStart);
if (queueStart < 0 || queueEnd < queueStart) throw new Error('CBZ image queue not found');
const queueTest = new Function('assert', `
    const document = { hidden: false };
    const window = { innerHeight: 800 };
    ${source.slice(queueStart, queueEnd)}
    const pages = Array.from({ length: 12 }, (_, index) => ({
        isConnected: true, dataset: {},
        getBoundingClientRect: () => ({ top: index * 1000, bottom: (index + 1) * 1000 }),
        loadImage() { this.dataset.loading = 'true'; return new Promise(() => {}); },
    }));
    const next = {
        isConnected: true, dataset: { prefetchNext: 'true' },
        getBoundingClientRect: () => ({ top: 20000, bottom: 21000 }),
        loadImage() { this.dataset.loading = 'true'; return new Promise(() => {}); },
    };
    pages.forEach(page => cbzImageQueue.add(page));
    cbzImageQueue.add(next);
    drainCbzImageQueue();
    assert.equal(cbzImagesLoading, 6);
    assert.equal(next.dataset.loading, 'true', 'next chapter first page should outrank distant current pages');
    assert.equal(pages[0].dataset.loading, 'true', 'visible current page remains prioritized');
    assert.equal(pages[11].dataset.loading, undefined);
`);
queueTest(assert);
assert.match(source, /if \(background && pageIndex < 2\) container\.dataset\.prefetchNext = 'true'/);
console.log('Reader fetches the next CBZ first pages ahead of distant current pages');
