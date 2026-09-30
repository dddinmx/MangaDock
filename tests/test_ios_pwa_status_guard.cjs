const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(
    path.join(__dirname, '../static/js/ios-pwa-status-guard.js'), 'utf8',
);

function run({standalone = true, safeTop = 62, safeBottom = 34, height = 956} = {}) {
    const properties = new Map();
    let probeAdded = false;
    const viewport = {content: 'width=device-width, initial-scale=1.0, viewport-fit=cover'};
    const probe = {style: {}, setAttribute() {}, remove() { probeAdded = false; }};
    const document = {
        hidden: false,
        querySelector: () => viewport,
        createElement: () => probe,
        body: {appendChild() { probeAdded = true; }},
        documentElement: {style: {setProperty: (name, value) => properties.set(name, value)}},
        addEventListener() {}, removeEventListener() {},
    };
    const context = {
        document,
        window: {
            matchMedia: () => ({matches: standalone}),
            getComputedStyle: () => ({paddingTop: `${safeTop}px`, paddingBottom: `${safeBottom}px`}),
            addEventListener() {}, removeEventListener() {},
        },
        navigator: {userAgent: 'iPhone', platform: 'iPhone', maxTouchPoints: 5},
        screen: {width: 440, height: 956},
        innerWidth: 440,
        innerHeight: height,
    };
    vm.runInNewContext(source, context);
    return {viewport, properties, probeAdded};
}

const overlapped = run();
assert.equal(overlapped.viewport.content, 'width=device-width, initial-scale=1.0');
assert.equal(overlapped.properties.get('--md-pwa-safe-bottom'), '34px');
assert.equal(overlapped.probeAdded, false);

for (const options of [{safeTop: 0}, {height: 700}, {standalone: false}]) {
    const normal = run(options);
    assert.match(normal.viewport.content, /viewport-fit=cover/);
    assert.equal(normal.properties.size, 0);
}
