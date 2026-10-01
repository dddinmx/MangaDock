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
let finished = 0;
vm.runInNewContext(source.slice(start, end) + '\nsettleSlide(1, "/cover.jpg", true, 7, 1, 420);', {
    mobileArt: false, nextArt: parts[1], nextBackground: null, movingParts: () => parts,
    slideEasing: 'ease', Promise,
    setProgress: () => parts.forEach((part, index) => {part.style.transform = `translateX(${index % 2 ? 0 : -100}%)`;}),
    finishSlide: (index, url, committed, token) => {
        assert.equal(index, 1); assert.equal(committed, true); assert.equal(token, 7); finished++;
    },
});
assert.equal(animations.length, 4, 'Covers and copy must all have explicit animations');
assert.equal(finished, 0, 'Slide must wait for actual animation completion');
animations.forEach(animation => {
    assert.equal(animation.options.duration, 420);
    assert.notEqual(animation.frames[0].transform, animation.frames[1].transform);
    animation.resolve();
});
setImmediate(() => {
    assert.equal(finished, 1);
    assert(animations.every(animation => animation.cancelled));
    console.log('Desktop hero animates all layers and settles after animation completion');
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
