const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const listeners = new Map(), heroListeners = new Map(), frames = new Map(), animations = [];
let nextFrame = 0, mobileChange, reducedChange, active = 0, detailOpen = false;
let sizeReads = 0, transformWrites = 0;
const mobile = {matches: true, addEventListener: (_, fn) => {mobileChange = fn;}};
const reduced = {matches: false, addEventListener: (_, fn) => {reducedChange = fn;}};
const images = [0, 1].map(() => ({
    get clientHeight() {sizeReads++; return 480;},
    style: {transform: '', transformOrigin: '', transition: ''},
    src: '/full-quality-cover.jpg',
    animate(keyframes, options) {
        let finish;
        const animation = {keyframes, options, cancelled: false,
            finished: new Promise(resolve => {finish = resolve;}),
            cancel() {this.cancelled = true;}, finish: () => finish()};
        animations.push(animation);
        return animation;
    },
}));
images.forEach(image => {
    let transform = '';
    Object.defineProperty(image.style, 'transform', {
        enumerable: true,
        get: () => transform,
        set(value) {transformWrites++; transform = value;},
    });
});
const hero = {
    dataset: {},
    querySelector: selector => selector.includes('native-panel') ? images[active] : images[0],
    addEventListener: (name, fn, options) => {
        heroListeners.set(name, fn);
        if (name === 'touchstart') assert.equal(options.passive, true);
    },
};
const context = {
    document: {
        querySelector: () => hero, hidden: false,
        documentElement: {dataset: {}},
        body: {classList: {contains: () => detailOpen}},
        addEventListener: (name, fn) => listeners.set(name, fn),
    },
    window: {
        scrollY: 0,
        matchMedia: query => query.includes('reduce') ? reduced : mobile,
        addEventListener: (name, fn, options) => {
            listeners.set(name, fn);
            if (name === 'scroll' || name.startsWith('touch')) assert.equal(options.passive, true);
        },
        requestAnimationFrame: fn => {frames.set(++nextFrame, fn); return nextFrame;},
        cancelAnimationFrame: id => frames.delete(id),
    },
};
const source = fs.readFileSync('static/js/home-hero-pull.js', 'utf8');
vm.runInNewContext(source, context);
const flush = () => {for (const [id, fn] of frames) {frames.delete(id); fn();}};
const scroll = y => {context.window.scrollY = y; listeners.get('scroll')(); flush();};
const touch = (name, x, y, count = 1) => {
    const event = {touches: Array.from({length: count}, () => ({clientX: x, clientY: y})),
        preventDefault() {throw new Error('Native gestures must remain passive');}};
    (name === 'touchstart' ? heroListeners : listeners).get(name)(event);
};
const pull = (distance = 96) => {
    scroll(0); touch('touchstart', 200, 200); touch('touchmove', 200, 240); scroll(-distance);
};

(async () => {
    flush();
    assert.equal(sizeReads, 0, 'Default off does not prepare geometry');
    assert.equal(hero.dataset.pullZoomEnabled, 'false', 'Default off does not promote zoom layers');
    const disabledFrames = nextFrame;
    pull(); touch('touchend');
    assert.equal(nextFrame, disabledFrames, 'Default off schedules no pull or release frames');
    assert.equal(transformWrites, 0);
    assert.equal(animations.length, 0);
    context.document.documentElement.dataset.coverMotion = 'on';
    listeners.get('md-cover-motion-change')(); flush();
    assert.equal(hero.dataset.pullZoomEnabled, 'true');
    assert.equal(sizeReads, 1, 'Cover geometry is prepared before the gesture');
    const initialWrites = transformWrites, initialFrames = nextFrame;
    for (const y of [100, 0, -4, -20, -8, 0]) scroll(y);
    assert.equal(nextFrame, initialFrames, 'Inertial top bounce schedules no zoom frames');
    assert.equal(transformWrites, initialWrites, 'Inertial top bounce never changes the cover');

    touch('touchstart', 200, 200);
    touch('touchmove', 130, 230); scroll(-12);
    touch('touchmove', 120, 350); scroll(-96);
    assert.equal(images[0].style.transform, '', 'Horizontal swipe stays locked even if it then moves down');
    assert.equal(nextFrame, initialFrames, 'Mixed horizontal gesture schedules no zoom frames');
    touch('touchend');
    assert.equal(animations.length, 0);

    scroll(0); touch('touchstart', 200, 200);
    touch('touchmove', 200, 160); scroll(-48);
    touch('touchmove', 200, 280); scroll(-96);
    touch('touchend');
    assert.equal(images[0].style.transform, '', 'An upward gesture cannot become a pull during bounce');

    pull();
    assert.equal(images[0].style.transform, 'translateZ(0) scale(1.2)');
    assert.equal(images[0].style.transition, '');
    assert.equal(images[0].src, '/full-quality-cover.jpg');
    assert.equal(images[1].style.transform, '', 'Only the selected cover is transformed');
    context.window.scrollY = -120;
    listeners.get('scroll')(); listeners.get('scroll')();
    assert.equal(frames.size, 1, 'Active pull scroll events are coalesced');
    flush();
    assert.equal(images[0].style.transform, 'translateZ(0) scale(1.25)');
    scroll(-10000);
    assert.equal(images[0].style.transform, 'translateZ(0) scale(1.35)');
    const writesAtLimit = transformWrites;
    scroll(-10001);
    assert.equal(transformWrites, writesAtLimit, 'Identical capped scales do not rewrite styles');
    assert.equal(sizeReads, 1, 'Pull frames never read layout geometry');
    touch('touchend');
    assert.equal(images[0].style.transform, '');
    assert.equal(animations.length, 1, 'Release uses one browser animation');
    assert.equal(animations[0].keyframes[0].transform, 'translateZ(0) scale(1.35)');
    assert.equal(animations[0].keyframes[1].transform, 'translateZ(0) scale(1)');
    assert.equal(animations[0].options.duration, 240);
    const releaseFrames = nextFrame, releaseWrites = transformWrites;
    for (const y of [-160, -100, -32, 0, -6, 0]) scroll(y);
    assert.equal(nextFrame, releaseFrames, 'Release inertia causes no JS animation frames');
    assert.equal(transformWrites, releaseWrites, 'Release inertia causes no style writes');
    animations[0].finish(); await Promise.resolve();
    assert.equal(animations[0].cancelled, true, 'Finished animations are cleaned up');

    scroll(160); touch('touchstart', 200, 200);
    touch('touchmove', 200, 240); scroll(60);
    touch('touchmove', 200, 300); scroll(0);
    touch('touchmove', 200, 330); scroll(-4);
    assert.equal(images[0].style.transform, '', 'Reaching the top is not itself a pull');
    touch('touchmove', 200, 360); scroll(-48);
    assert.equal(images[0].style.transform, 'translateZ(0) scale(1.1)', 'Continuing downward at the top zooms');
    touch('touchend');
    pull();
    assert.equal(animations[1].cancelled, true, 'A new gesture cancels the previous settling animation');
    touch('touchmove', 200, 260, 2);
    assert.equal(images[0].style.transform, '', 'Multiple fingers cancel zoom');
    scroll(-96);
    assert.equal(frames.size, 0);

    active = 1;
    heroListeners.get('md-home-hero-change')(); flush();
    pull(48);
    assert.equal(images[1].style.transform, 'translateZ(0) scale(1.1)');
    heroListeners.get('md-home-hero-change')(); flush();
    assert.equal(images[1].style.transform, '', 'Cover changes cancel the old pull');
    reduced.matches = true; reducedChange(); flush();
    pull(); assert.equal(images[1].style.transform, '');
    reduced.matches = false;
    mobile.matches = false; mobileChange(); flush();
    pull(); assert.equal(images[1].style.transform, '');
    mobile.matches = true;
    detailOpen = true;
    pull(); assert.equal(images[1].style.transform, '');
    detailOpen = false;
    pull(); touch('touchcancel');
    assert.equal(images[1].style.transform, '');
    pull(); context.document.hidden = true; listeners.get('visibilitychange')();
    assert.equal(images[1].style.transform, '');
    context.document.hidden = false; listeners.get('visibilitychange')(); flush();
    pull(); touch('touchend');
    const lastAnimation = animations.at(-1);
    listeners.get('pagehide')();
    assert.equal(lastAnimation.cancelled, true);
    pull(); listeners.get('resize')();
    assert.equal(frames.size, 1, 'Resize replaces the pull frame with geometry preparation');
    flush();
    assert.equal(images[1].style.transform, '');
    pull(); touch('touchend');
    const enabledAnimation = animations.at(-1);
    context.document.documentElement.dataset.coverMotion = 'off';
    listeners.get('md-cover-motion-change')();
    assert.equal(enabledAnimation.cancelled, true, 'Turning off cancels an existing release');
    assert.equal(hero.dataset.pullZoomEnabled, 'false');
    assert.equal(frames.size, 0);
    const offFrames = nextFrame, offWrites = transformWrites, offReads = sizeReads;
    pull(); scroll(-120); touch('touchend');
    assert.equal(nextFrame, offFrames);
    assert.equal(transformWrites, offWrites);
    assert.equal(sizeReads, offReads, 'Disabled motion has no geometry work');
    vm.runInNewContext(source, context);
    assert.equal(hero.dataset.pullZoomBound, 'true');
    console.log('Default-off motion, immediate cancellation, intentional pull and navigation guards passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
