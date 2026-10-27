const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/js/home-detail-navigation.js', 'utf8');
const start = source.indexOf("    document.addEventListener('click', event => {");
const end = source.indexOf("    window.addEventListener('popstate'", start);
function check(returnByPush) {
    let handler, pushes = 0, backs = 0, slides = 0, prevented = false;
    const link = {href: 'http://localhost/', target: '', hasAttribute: () => false,
        matches: selector => selector === '.md-detail-back'};
    const context = {URL, location: {origin: 'http://localhost', href: 'http://localhost/comic/example', pathname: '/comic/example'},
        document: {addEventListener: (_, fn) => handler = fn},
        homeUrl: 'http://localhost/', home: {contains: () => false},
        shell: {contains: () => true}, pending: null, active: true, motion: null, homeRequested: false, ready: true,
        returnByPush, showHome: () => slides++, state: type => ({type}),
        history: {back: () => backs++, pushState: (state, _, url) => {
            assert.equal(state.type, 'home'); assert.equal(url, 'http://localhost/'); pushes++;
        }}
    };
    const requestStart = source.indexOf('    function requestHome() {');
    const requestEnd = source.indexOf('    function settleSwipe()', requestStart);
    vm.runInNewContext(source.slice(requestStart, requestEnd) + source.slice(start, end), context);
    const event = {target: {closest: () => link}, button: 0, preventDefault: () => prevented = true};
    handler(event);
    assert(prevented);
    assert.equal(backs, returnByPush ? 0 : 1, 'Reader-origin detail must not back into reader');
    assert.equal(pushes, returnByPush ? 1 : 0);
    assert.equal(slides, returnByPush ? 1 : 0);
    context.motion = {};
    handler(event);
    assert.equal(pushes + backs, 1, 'Repeated taps during a slide do not change history');
}
check(false); check(true);
const animationStart = source.indexOf('    async function animate(enter) {');
const animationEnd = source.indexOf('    async function showDetail()', animationStart);
async function checkAnimation(mobile, reduced, enter) {
    const calls = [], fades = [], timers = new Map(), listeners = new Map();
    let timerId = 0;
    let finishFade, fadeCancelled = false;
    const shell = {animate(keyframes, options) {
        fades.push({keyframes, options});
        return {finished: new Promise(resolve => { finishFade = resolve; }),
            cancel() { fadeCancelled = true; }};
    }};
    const viewport = {clientWidth: 390, scrollLeft: enter ? 0 : 390,
        scrollTo(options) { calls.push(options); this.scrollLeft = options.left; },
        addEventListener(name, fn) { listeners.set(name, fn); },
        removeEventListener(name) { listeners.delete(name); }};
    const context = {mobile: {matches: mobile}, reduced: {matches: reduced}, motion: null,
        viewport, shell, home: shell, touching: false, Promise,
        setTimeout: (fn, ms) => { timers.set(++timerId, {fn, ms}); return timerId; },
        clearTimeout: id => timers.delete(id)};
    const completion = vm.runInNewContext(source.slice(animationStart, animationEnd)
        + `\nanimate(${enter});`, context);
    if (mobile) {
        assert.equal(calls.length, 1, 'Native scrolling starts immediately without two animation frames');
        assert.equal(calls[0].left, enter ? 390 : 0);
        assert.equal(calls[0].behavior, 'auto', 'Both directions switch without an automatic slide');
        if (!reduced) {
            assert.equal(fades.length, 1, 'The destination is visible immediately while the brief fade runs');
            assert.equal(fades[0].options.duration, 140);
            assert(fades[0].keyframes.every(frame => !('transform' in frame)), 'Neither direction slides or scales');
            assert.equal(listeners.size, 0, 'Page changes do not wait for native scroll settling');
            finishFade();
        }
    } else assert.equal(calls.length, 0, 'Desktop navigation does not scroll sideways');
    await completion;
    assert.equal(fades.length, mobile && !reduced ? 1 : 0);
    assert.equal(fadeCancelled, fades.length > 0, 'Finished fades release their animation object');
    assert.equal(listeners.size, 0, 'Temporary scroll completion listeners are removed');
    assert.equal(timers.size, 0);
}
const swipeStart = source.indexOf('    function requestHome() {');
const swipeEnd = source.indexOf("    viewport.addEventListener('touchstart'", swipeStart);
function checkSwipe(returnByPush) {
    let backs = 0, pushes = 0, closes = 0;
    const context = {returnByPush, homeRequested: false, homeUrl: 'http://localhost/',
        touching: true, motion: null, active: true, mobile: {matches: true}, ready: true,
        viewport: {scrollLeft: 0}, settleTimer: null, clearTimeout() {},
        history: {back: () => backs++, pushState: () => pushes++},
        state: type => ({type}), showHome: () => closes++};
    vm.runInNewContext(source.slice(swipeStart, swipeEnd), context);
    context.settleSwipe();
    assert.equal(backs + pushes, 0, 'Do not change history while the finger is still down');
    context.touching = false;
    context.viewport.scrollLeft = 390;
    context.settleSwipe();
    assert.equal(backs + pushes, 0, 'A cancelled swipe keeps the detail open');
    context.viewport.scrollLeft = 0;
    context.settleSwipe(); context.settleSwipe();
    assert.equal(backs, returnByPush ? 0 : 1);
    assert.equal(pushes, returnByPush ? 1 : 0);
    assert.equal(closes, returnByPush ? 1 : 0);
    assert.equal(backs + pushes, 1, 'Scrollend and its fallback commit one history change');
}
checkSwipe(false); checkSwipe(true);
Promise.all([checkAnimation(false, false, true), checkAnimation(true, false, true),
    checkAnimation(true, false, false), checkAnimation(true, true, true), checkAnimation(true, true, false)]).then(() => {
    console.log('Immediate detail entry, brief fade, native swipe return, history and reduced motion passed');
}).catch(error => { console.error(error); process.exitCode = 1; });
