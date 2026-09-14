// Run: node --test tests/novel-reader-motion.test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../templates/novel_reader.html'), 'utf8');
function source(name) {
    const start = html.indexOf(`        function ${name}(`);
    assert.ok(start >= 0, name);
    const rest = html.slice(start);
    const end = rest.slice(1).search(/\n        (?:async )?function /);
    return end < 0 ? rest : rest.slice(0, end + 1);
}
function harness(names, values = {}) {
    const timers = new Map();
    let sequence = 0;
    const context = vm.createContext({
        pageStep: 390, renderedFlowOffset: -120,
        viewport: { clientWidth: 390, clientHeight: 720 },
        isTouching: false, flowIsSettling: false, isSwitchingChapter: false,
        paginationTimer: null, resizeTimer: null,
        measuredViewportWidth: 390, measuredViewportHeight: 720,
        setTimeout(callback) { const id = ++sequence; timers.set(id, callback); return id; },
        clearTimeout(id) { timers.delete(id); },
        ...values,
    });
    names.forEach(name => vm.runInContext(source(name), context));
    return {
        context,
        run: code => vm.runInContext(code, context),
        tick() { const tasks = [...timers.values()]; timers.clear(); tasks.forEach(callback => callback()); },
        timers,
    };
}
function curve(motion) {
    return motion.easing.slice('cubic-bezier('.length, -1).split(',').map(Number);
}

test('release speed is continuous for slow, medium and fast swipes in both directions', () => {
    const { run } = harness(['settleMotion']);
    for (const direction of [-1, 1]) {
        for (const speed of [.2, .8, 2]) {
            const motion = run(`settleMotion(${direction * 270}, ${direction * speed}, 0)`);
            const [x1, y1, x2, y2] = curve(motion);
            const startSpeed = 270 / motion.duration * y1 / x1;
            assert.ok(Math.abs(startSpeed - speed) < 1e-10);
            assert.equal((1 - y2) / (1 - x2), 0, 'ends at rest');
        }
    }
});

test('short and extreme flicks remain bounded and never overshoot', () => {
    const { run } = harness(['settleMotion']);
    for (const distance of [1, 10, 100, 390]) {
        for (const speed of [0, .2, 2, 20, -2]) {
            const motion = run(`settleMotion(${distance}, ${speed}, 0)`);
            const [, y1, , y2] = curve(motion);
            assert.ok(motion.duration >= 120 && motion.duration <= 350);
            let previous = 0;
            for (let step = 0; step <= 100; step++) {
                const t = step / 100;
                const position = 3 * (1 - t) ** 2 * t * y1 + 3 * (1 - t) * t ** 2 * y2 + t ** 3;
                assert.ok(position >= previous - 1e-12 && position <= 1);
                previous = position;
            }
        }
    }
    assert.equal(run('settleMotion(0, 2, 0).duration'), 0);
});

test('resize is coalesced and does no layout work during touch or settling', () => {
    let applied = 0;
    const h = harness(['isMotionBusy', 'handleReaderResize'], { applyReaderMargin() { applied++; } });
    h.run('isTouching = true; viewport.clientHeight = 680; handleReaderResize(); handleReaderResize()');
    assert.equal(h.timers.size, 1);
    h.tick();
    assert.equal(applied, 0);
    h.run('isTouching = false; flowIsSettling = true');
    h.tick();
    assert.equal(applied, 0);
    h.run('flowIsSettling = false');
    h.tick();
    assert.equal(applied, 1);
});

test('resize without a reading-area size change does not invalidate cached chapters', () => {
    let applied = 0;
    const h = harness(['isMotionBusy', 'handleReaderResize'], { applyReaderMargin() { applied++; } });
    h.run('handleReaderResize()');
    h.tick();
    assert.equal(applied, 0);
    h.run('viewport.clientWidth = 720; handleReaderResize()');
    h.tick();
    assert.equal(applied, 1);
});

test('pagination waits through dragging, settling and chapter switching, preserving its target page', () => {
    const requests = [];
    const h = harness(['isMotionBusy', 'recalculatePagination'], {
        schedulePagination(...args) { requests.push(args); },
    });
    for (const state of ['isTouching', 'flowIsSettling', 'isSwitchingChapter']) {
        h.run(`${state} = true; recalculatePagination(false, 7); ${state} = false`);
    }
    assert.deepEqual(requests, [[false, 7], [false, 7], [false, 7]]);
});

test('animation finish defers storage and a new gesture cancels pending layer demotion', () => {
    let saved = 0;
    let released = 0;
    const element = { classList: { add() {} } };
    const h = harness(['isMotionBusy', 'settleMotion', 'setPage', 'addMotionHint'], {
        pageIndex: 0, pageCount: 10, flow: element,
        motionHintReleaseTimer: null, motionHintElements: new Set(),
        setFlowPosition(offset, smooth, duration, easing, velocity, done) { done(); },
        releaseMotionHint() { released++; }, updatePageLabel() {},
        persistBrowserProgress() { throw new Error('storage must not run on the finish frame'); },
        queueSave(options) { assert.equal(options.skipAnchor, true); saved++; },
    });
    h.run('setPage(1, true, -.8)');
    assert.equal(saved, 1);
    assert.equal(released, 0);
    h.run('isTouching = true; addMotionHint(flow)');
    h.tick();
    assert.equal(released, 0);
    h.run('isTouching = false; setPage(2, true, -.8)');
    h.tick();
    assert.equal(released, 1);
});

test('complete reader script parses after substituting template data', () => {
    for (const match of html.matchAll(/<script>([\s\S]*?)<\/script>/g)) {
        new vm.Script(match[1].replace(/\{\{[\s\S]*?\}\}/g, '0'));
    }
});

test('release discards unpainted drag coordinates without jumping either chapter layer', () => {
    const cancelled = [];
    const h = harness(['cancelQueuedDrag', 'finishGestureDrag'], {
        renderedFlowOffset: -82, renderedLayerOffset: -82,
        pendingDragOffset: -130, dragAnimationFrame: 1,
        pendingLayerDrag: { outgoingOffset: -130, incomingOffset: 260 }, layerDragAnimationFrame: 2,
        cancelAnimationFrame(id) { cancelled.push(id); },
        applyFlowOffset() { throw new Error('release must not snap the flow'); },
    });
    h.run('finishGestureDrag()');
    assert.deepEqual(cancelled, [1, 2]);
    assert.equal(h.run('renderedFlowOffset'), -82);
    assert.equal(h.run('renderedLayerOffset'), -82);
    assert.equal(h.run('pendingDragOffset'), null);
    assert.equal(h.run('pendingLayerDrag'), null);
});

test('normal lift keeps velocity, but a pause before lift stops inertia', () => {
    const h = harness(['releaseTouchVelocity'], { touchLastTime: 100, touchVelocityX: -1.2 });
    assert.equal(h.run('releaseTouchVelocity(116)'), -1.2);
    assert.equal(h.run('releaseTouchVelocity(132)'), -1.2);
    assert.equal(Math.abs(h.run('releaseTouchVelocity(222)')), 0);
});

test('touch timestamps use event sampling time rather than delayed handler execution', () => {
    const h = harness(['touchEventTime'], { performance: { now: () => 900 } });
    assert.equal(h.run('touchEventTime({timeStamp: 110})'), 110);
    assert.equal(h.run('touchEventTime({})'), 900);
});

test('actual touchend handler settles from submitted position while final coordinates choose the page', () => {
    const match = html.match(/viewport\.addEventListener\('touchend', \(event\) => \{([\s\S]*?)\n        \}, \{passive: true\}\);/);
    assert.ok(match);
    let moved = 0;
    const h = harness(['cancelQueuedDrag', 'finishGestureDrag', 'touchEventTime', 'releaseTouchVelocity'], {
        touchStartX: 300, touchStartY: 200, touchStartTime: 10,
        touchAxis: 'horizontal', touchDeltaX: -130, touchLastX: 170, touchLastTime: 100,
        touchVelocityX: -1, touchBaseOffset: 0, layerBaseOffset: 0,
        renderedFlowOffset: -82, renderedLayerOffset: 0,
        pendingDragOffset: -130, dragAnimationFrame: 1,
        layerDragAnimationFrame: null, pendingLayerDrag: null,
        pageIndex: 1, pageCount: 10, chapterIndex: 0, totalChapters: 3, pageWidth: 390,
        flow: {}, lastSwipeAt: 0,
        cancelAnimationFrame() {}, clearMotionHints() {},
        movePage(direction) { moved = direction; },
        applyFlowOffset() { throw new Error('unexpected touchend position jump'); },
    });
    h.run(`const onTouchEnd = event => {${match[1]}}; onTouchEnd({timeStamp: 116, changedTouches: [{clientX: 150}]})`);
    assert.equal(moved, 1);
    assert.equal(h.run('renderedFlowOffset'), -82);
    assert.equal(h.run('isTouching'), false);
});
