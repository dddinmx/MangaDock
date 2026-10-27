const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/js/home-detail-navigation.js', 'utf8');
const start = source.indexOf('    async function loadDetail(url, push) {');
const end = source.indexOf("    home.addEventListener('md-home-hero-change'", start);
function fixture() {
    const requests = [], mounted = [], initialized = [], slides = [], timers = new Map();
    const classes = new Set(), attributes = new Map();
    let parses = 0, timerId = 0;
    const context = {
        pending: null, warmed: null, warmTimer: null, active: false, currentUrl: null,
        mobile: {matches: true}, returnByPush: false, AbortController, Date, Promise,
        document: {hidden: false, importNode: content => content},
        home: {querySelector: () => ({href: 'http://localhost/comic/a'})},
        shell: {hidden: true, inert: false, replaceChildren: content => mounted.push(content.name),
            querySelectorAll: () => []},
        viewport: {hidden: true, classList: {add: name => classes.add(name)},
            setAttribute: (name, value) => attributes.set(name, value), scrollTo() {}},
        window: {initializeComicDetail: () => initialized.push(mounted.at(-1))},
        DOMParser: class {parseFromString(name) {
            parses++;
            return {title: name, querySelector: () => ({name,
                querySelector: () => ({}), querySelectorAll: () => []})};
        }},
        fetch: (url, options) => new Promise((resolve, reject) => {
            requests.push({url, signal: options.signal,
                resolve: () => resolve({ok: true, redirected: false, text: async () => url}), reject});
        }),
        showDetail: async () => slides.push(context.currentUrl), resetFeedback() {},
        history: {pushState: (_, __, url) => assert.equal(url, context.currentUrl)},
        state: () => ({}), location: {assign: url => { throw new Error(`Unexpected full navigation: ${url}`); }},
        setTimeout: (fn, ms) => { timers.set(++timerId, {fn, ms}); return timerId; },
        clearTimeout: id => timers.delete(id)
    };
    vm.runInNewContext(source.slice(start, end), context);
    const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
    return {context, requests, mounted, initialized, slides, timers, classes, attributes, flush,
        get parses() { return parses; }};
}
async function run() {
    const url = 'http://localhost/comic/a';
    const warm = fixture();
    warm.context.scheduleWarm();
    assert.equal(warm.requests.length, 0, 'Warm work waits until the current cover settles');
    [...warm.timers.values()].find(timer => timer.ms === 300).fn();
    assert.equal(warm.requests.length, 1);
    warm.requests[0].resolve(); await warm.flush();
    assert.deepEqual(warm.mounted, [url]);
    assert.deepEqual(warm.initialized, [url], 'Layout initialization happens before tapping');
    assert(warm.classes.has('is-preparing'));
    assert.equal(warm.attributes.get('aria-hidden'), 'true');
    assert(warm.context.shell.inert, 'Offscreen detail cannot receive keyboard focus');
    warm.context.warmDetail(url);
    assert.equal(warm.requests.length, 1, 'Pointerdown reuses the selected cover preparation');
    await warm.context.loadDetail(url, true);
    assert.deepEqual(warm.slides, [url]);
    assert.equal(warm.parses, 1, 'Opening a prepared detail does not parse its HTML again');
    assert.equal(warm.initialized.length, 1, 'Opening does not mount or bind the chapter list again');
    assert.equal(warm.requests.length, 1, 'Opening does not wait for a new request');
    for (let i = 0; i < 20; i++) {
        warm.context.warmDetail(url);
        assert.equal(warm.requests.length, 1, 'Returning to the same cover must not request its detail again');
        await warm.context.loadDetail(url, true);
    }
    assert.equal(warm.requests.length, 1, 'Twenty reopens reuse the retained detail instead of refetching');
    assert.equal(warm.initialized.length, 1, 'Repeated return and open do not rebuild the same detail');

    const race = fixture();
    race.context.warmDetail(url);
    const next = 'http://localhost/comic/b';
    race.context.warmDetail(next);
    assert(race.requests[0].signal.aborted);
    race.requests[1].resolve(); await race.flush();
    race.requests[0].resolve(); await race.flush();
    assert.deepEqual(race.mounted, [next], 'Late results for the previous cover cannot overwrite the next');
    await race.context.loadDetail(next, true);
    assert.deepEqual(race.slides, [next]);

    const early = fixture();
    early.context.warmDetail(url);
    const entry = early.context.loadDetail(url, true);
    early.requests[0].resolve(); await entry;
    assert.equal(early.requests.length, 1, 'An early tap shares the request already in flight');
    assert.deepEqual(early.mounted, [url]);

    const failed = fixture();
    failed.context.warmDetail(url);
    const retry = failed.context.loadDetail(url, true);
    failed.requests[0].reject(new Error('Warm request failed')); await failed.flush();
    assert.equal(failed.requests.length, 2, 'A failed warm request is retried on real navigation');
    failed.requests[1].resolve(); await retry;
    assert.deepEqual(failed.slides, [url]);

    const retained = fixture();
    retained.context.warmDetail(url);
    retained.requests[0].resolve(); await retained.flush();
    retained.context.Date = {now: () => Date.now() + 300000};
    retained.context.warmDetail(url);
    await retained.context.loadDetail(url, true);
    assert.equal(retained.requests.length, 1, 'Reading the visible cover does not expire its ready detail');

    const lifecycle = fixture();
    let visibility;
    lifecycle.context.document.addEventListener = (_, handler) => { visibility = handler; };
    const visibilityStart = source.indexOf("    document.addEventListener('visibilitychange'");
    const visibilityEnd = source.indexOf('    // Other library entries', visibilityStart);
    vm.runInNewContext(source.slice(visibilityStart, visibilityEnd), lifecycle.context);
    lifecycle.context.warmDetail(url);
    lifecycle.context.document.hidden = true;
    visibility();
    assert(lifecycle.requests[0].signal.aborted);
    lifecycle.requests[0].resolve(); await lifecycle.flush();
    assert.equal(lifecycle.mounted.length, 0, 'Hidden-page results cannot mount a prepared detail');
    assert.equal(lifecycle.context.warmed, null, 'Leaving the visible homepage invalidates its data');
    lifecycle.context.document.hidden = false;
    visibility();
    [...lifecycle.timers.values()].find(timer => timer.ms === 300).fn();
    assert.equal(lifecycle.requests.length, 2, 'Returning to the homepage prepares fresh detail data');

    for (const flag of ['desktop', 'hidden', 'active', 'pending']) {
        const excluded = fixture();
        if (flag === 'desktop') excluded.context.mobile.matches = false;
        if (flag === 'hidden') excluded.context.document.hidden = true;
        if (flag === 'active') excluded.context.active = true;
        if (flag === 'pending') excluded.context.pending = {};
        excluded.context.warmDetail(url);
        assert.equal(excluded.requests.length, 0, `No speculative requests while ${flag}`);
    }
    console.log('Selected-cover preparation, immediate reuse, races, early taps, retry and retention passed');
}
run().catch(error => { console.error(error); process.exitCode = 1; });
