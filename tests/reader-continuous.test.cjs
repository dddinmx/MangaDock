// Run with: node --test tests/reader-continuous.test.cjs
// Exercise the actual template script with deterministic page sizes and async image loading.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const template = fs.readFileSync(path.join(__dirname, '../templates/reader.html'), 'utf8');
const script = template.match(/<script>([\s\S]*?)<\/script>/)[1].replace(/\{\{[\s\S]*?\}\}/g, '0');

function reader(width = 390) {
    let context;
    class Element {
        constructor(tag) {
            this.tagName = tag;
            this.children = [];
            this.dataset = {};
            this.style = { setProperty() {} };
            this.classList = { add() {}, remove() {}, contains() { return false; } };
            this.events = {};
        }
        appendChild(child) { child.remove(); this.children.push(child); child.parentElement = this; return child; }
        append(...children) { children.forEach(child => this.appendChild(child)); }
        remove() {
            if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(child => child !== this);
            this.parentElement = null;
        }
        set innerHTML(value) { this.children.forEach(child => { child.parentElement = null; }); this.children = []; }
        replaceChildren(...children) { this.innerHTML = ''; this.append(...children); }
        addEventListener(name, callback) { this.events[name] = callback; }
        setAttribute() {}
        getContext() { return {}; }
        get offsetHeight() { return this.height || this.children.reduce((sum, child) => sum + child.offsetHeight, 0); }
        get top() {
            if (!this.parentElement) return 56;
            const siblings = this.parentElement.children;
            return this.parentElement.top + siblings.slice(0, siblings.indexOf(this)).reduce((sum, child) => sum + child.offsetHeight, 0);
        }
        getBoundingClientRect() { return { top: this.top - context.window.scrollY, bottom: this.top + this.offsetHeight - context.window.scrollY }; }
    }
    const elements = new Map();
    const document = {
        getElementById(id) { if (!elements.has(id)) elements.set(id, new Element('div')); return elements.get(id); },
        createElement(tag) { return new Element(tag); },
        addEventListener() {}, querySelectorAll() { return []; }, hidden: false,
    };
    document.body = document.documentElement = { get scrollHeight() { return document.getElementById('page-content').offsetHeight + 152; } };
    context = vm.createContext({
        document, console: { log() {}, error() {} }, setTimeout, clearTimeout,
        performance, URL, requestAnimationFrame: callback => setTimeout(callback, 0),
        window: {
            innerWidth: width, innerHeight: 800, scrollY: 0, devicePixelRatio: 1,
            addEventListener() {}, setTimeout, clearTimeout,
            requestAnimationFrame: callback => setTimeout(callback, 0),
            scrollTo(x, y) { this.scrollY = y; },
        },
        pdfjsLib: {
            GlobalWorkerOptions: {},
            getDocument() { return { promise: Promise.resolve({
                numPages: 2, destroy: async () => {},
                getPage: async () => ({
                    getViewport: ({ scale }) => ({ width: 390 * scale, height: 1200 * scale }),
                    render: () => ({ promise: Promise.resolve() }),
                }),
            }) }; },
        },
    });
    vm.runInContext(script, context);
    const run = code => vm.runInContext(code, context);
    run(`
        chapters = Array.from({length: 4}, (_, index) => ({title: 'Chapter ' + index, filename: index + '.cbz', format: 'cbz'}));
        queueProgressUiSync = () => {};
        prefetchNextCbzChapter = () => {};
        ensureCbzChapterCache = async chapter => ({images: ['1', '2', '3'], chapter});
        buildCbzPageContainer = async (cache, image) => {
            const element = document.createElement('div');
            element.height = 1000;
            element.dataset.image = cache.chapter.filename + '/' + image;
            return element;
        };
    `);
    return { run, context, elements };
}

test('mobile appends complete chapters without replacing pages or resetting scroll', async () => {
    const { run } = reader();
    await run('loadChapter(0, false)');
    run('window.scrollY = 1800');
    const original = run('pageContent.children[0]');
    await run('appendNextChapter()');
    assert.equal(run('pageContent.children[0]'), original);
    assert.equal(run('window.scrollY'), 1800);
    assert.equal(run('currentChapter'), 0);
    assert.equal(run('continuousSections.length'), 2);
    assert.equal(run('continuousSections[1].element.children.length'), 3);
    assert.equal(run('continuousSections[1].element.children[0].dataset.image'), '1.cbz/1');
    run('window.scrollY = 3200; syncContinuousReading()');
    assert.equal(run('currentChapter'), 1);
    assert.equal(run('headerChapter.textContent'), 'Chapter 1');
    assert.equal(JSON.parse(run('buildProgressPayload()')).scroll_position, 200);
    run('window.scrollY = 1000; syncContinuousReading()');
    assert.equal(run('currentChapter'), 0);
    run('currentChapter = 1; scrollToProgressValue(500)');
    assert.equal(run('getProgressPercent()'), 50);
});

test('desktop never appends when scrolling to the end', async () => {
    const { run } = reader(1280);
    await run('loadChapter(0, false)');
    run('window.scrollY = 3000; syncContinuousReading()');
    await run('appendNextChapter()');
    assert.equal(run('continuousSections.length'), 0);
    assert.equal(run('currentChapter'), 0);
});

test('approaching the chapter end automatically loads the next chapter only after all pages are ready', async () => {
    const { run } = reader();
    await run('loadChapter(0, false)');
    run('syncContinuousReading()');
    assert.equal(run('continuousLoadPending'), false);
    run('window.scrollY = 1800; continuousSections[0].ready = false; syncContinuousReading()');
    assert.equal(run('continuousLoadPending'), false);
    run('continuousSections[0].ready = true; syncContinuousReading()');
    assert.equal(run('continuousLoadPending'), true);
    for (let attempt = 0; attempt < 50 && run('continuousLoadPending'); attempt++) {
        await new Promise(resolve => setTimeout(resolve, 5));
    }
    assert.equal(run('continuousSections.length'), 2);
    assert.equal(run('window.scrollY'), 1800);
});

test('concurrent requests append once and manual navigation invalidates pending work', async () => {
    const { run } = reader();
    await run('loadChapter(0, false)');
    run('let release; const originalCache = ensureCbzChapterCache; ensureCbzChapterCache = chapter => new Promise(resolve => { release = () => resolve(originalCache(chapter)); })');
    const pending = run('appendNextChapter()');
    await run('appendNextChapter()');
    assert.equal(run('continuousSections.length'), 1);
    run('release()');
    await pending;
    assert.equal(run('continuousSections.length'), 2);
    const stale = run('appendNextChapter()');
    run('ensureCbzChapterCache = originalCache');
    await run('loadChapter(3, false)');
    run('release()');
    await stale;
    assert.equal(run('continuousSections.length'), 1);
    assert.equal(run('continuousSections[0].index'), 3);
    assert.equal(run('continuousLoadPending'), false);
});

test('failure preserves current chapter, blocks retry loops, and allows retry', async () => {
    const { run } = reader();
    await run('loadChapter(0, false)');
    run('const originalCache = ensureCbzChapterCache; ensureCbzChapterCache = async () => { throw new Error("offline"); }');
    await run('appendNextChapter()');
    assert.equal(run('continuousLoadFailed'), true);
    assert.equal(run('continuousSections.length'), 1);
    assert.equal(run('pageContent.children[1].textContent'), '下一章加载失败，点击重试');
    run('ensureCbzChapterCache = originalCache; continuousLoadFailed = false; pageContent.children[1].remove()');
    await run('appendNextChapter()');
    assert.equal(run('continuousSections.length'), 2);
});

test('last chapter stops appending and restored chapter uses local scroll position', async () => {
    const { run } = reader();
    run('savedScrollPosition = 450');
    await run('loadChapter(3, true)');
    await run('appendNextChapter()');
    assert.equal(run('continuousSections.length'), 1);
    assert.equal(run('window.scrollY'), 450);
    assert.equal(JSON.parse(run('buildProgressPayload()')).scroll_position, 450);
});

test('PDF chapters append rendered pages and update the visible chapter', async () => {
    const { run } = reader();
    run('chapters.forEach(chapter => { chapter.format = "pdf"; })');
    await run('loadChapter(0, false)');
    await run('appendNextChapter()');
    assert.equal(run('continuousSections.length'), 2);
    assert.equal(run('continuousSections[1].element.children.length'), 2);
    run('window.scrollY = continuousSections[0].element.offsetHeight + 100; syncContinuousReading()');
    assert.equal(run('currentChapter'), 1);
    assert.equal(JSON.parse(run('buildProgressPayload()')).scroll_position, 100);
});

test('short chapters prefetch at most one chapter beyond the visible chapter', async () => {
    const { run } = reader();
    run('buildCbzPageContainer = async () => { const element = document.createElement("div"); element.height = 20; return element; }');
    await run('loadChapter(0, false)');
    await run('appendNextChapter()');
    run('syncContinuousReading()');
    assert.equal(run('continuousLoadPending'), false);
    assert.equal(run('continuousSections.length'), 2);
});
