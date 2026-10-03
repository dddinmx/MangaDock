const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/js/home-hero-carousel.js', 'utf8');
const start = source.indexOf('    const settleSlide =');
const end = source.indexOf('    const animateSlide =', start);
const animations = [];
const parts = [0, 100, 0, 100].map(x => ({
    style: {transform: `translateX(${x}%)`},
    animate(frames, options) {
        let resolve;
        const animation = {frames, options, finished: new Promise(done => {resolve = done;}),
            resolve: () => resolve(), cancel: () => {animation.cancelled = true;}};
        animations.push(animation);
        return animation;
    },
}));
const background = {animate: parts[0].animate};
const nextBackground = {animate: parts[0].animate};
let finished = 0;
vm.runInNewContext(source.slice(start, end) + '\nsettleSlide(1, "/cover.jpg", true, 7, 1, 360);', {
    mobileArt: false, nextArt: parts[1], background, nextBackground, movingParts: () => parts,
    getComputedStyle: () => {throw Error('Animation startup must not read computed styles');},
    slideEasing: 'ease', Promise,
    setProgress: () => {throw Error('Browser animation must not write end transforms before starting');},
    finishSlide: (index, url, committed, token) => {
        assert.equal(index, 1); assert.equal(committed, true); assert.equal(token, 7); finished++;
    },
});
assert.equal(animations.length, 6, 'Covers, copy and blurred backgrounds animate together');
assert.equal(animations[4].frames[0].opacity, '0.9');
assert.equal(animations[5].frames[0].opacity, '0.001');
assert.equal(finished, 0, 'Slide must wait for actual animation completion');
animations.forEach(animation => {
    assert.equal(animation.options.duration, 360);
    if (animation.frames[0].transform) assert.notEqual(animation.frames[0].transform, animation.frames[1].transform);
    animation.resolve();
});
setImmediate(() => {
    assert.equal(finished, 1);
    assert(animations.every(animation => animation.cancelled));
    console.log('Desktop animation starts without style reads or endpoint writes, then settles all six layers');
});

async function checkMotionPreference() {
    const selection = source.slice(source.indexOf('    const selectSlide ='), source.indexOf('    const move ='));
    for (const reducedMotion of [false, true]) {
        let slides = 0, jumps = 0;
        vm.runInNewContext(selection + '\nselectSlide(1, 1);', {
            slides: [0, 1], reducedMotion, mobileArt: false,
            requestId: 0, requestedIndex: 0, requestedDirection: 1, sliding: false, activeIndex: 0,
            hero: {classList: {toggle() {}, remove() {}}}, zone: {setAttribute() {}},
            readyUrl: () => '/cover.jpg', warmArt: () => Promise.resolve('/cover.jpg'),
            showSlide: () => jumps++, animateSlide: () => slides++,
        });
        await Promise.resolve();
        assert.equal(slides, 1, 'Manual desktop cover switching must slide for either motion preference');
        assert.equal(jumps, 0, 'Desktop cover switching must not silently jump');
    }
}
checkMotionPreference().catch(error => {console.error(error); process.exitCode = 1;});

// Clicking a cover already staged during idle must not reset its blurred image source.
const stage = source.slice(source.indexOf('    const stageSlide ='), source.indexOf('    const prepareSlide ='));
let sourceWrites = 0;
const standby = {style: {}, getAttribute: () => '/cover.jpg', set src(_) {sourceWrites++;}};
vm.runInNewContext(stage + '\nstageSlide(1, "/cover.jpg", 1);', {
    background: {style: {}}, nextBackground: standby, stagedIndex: 1,
    movingParts: () => parts, setProgress() {},
});
assert.equal(sourceWrites, 0, 'Prepared blurred cover source is reused on click');
