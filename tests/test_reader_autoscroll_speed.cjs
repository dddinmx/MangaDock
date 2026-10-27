const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../templates/reader.html'), 'utf8');

function functionSource(name) {
    const start = source.indexOf(`function ${name}(`);
    assert.ok(start >= 0, `${name} exists`);
    const bodyStart = source.indexOf('{', start);
    let depth = 0;
    for (let index = bodyStart; index < source.length; index++) {
        if (source[index] === '{') depth++;
        if (source[index] === '}' && --depth === 0) return source.slice(start, index + 1);
    }
    throw new Error(`Unclosed function: ${name}`);
}

const createReaderSpeed = new Function('storage', `
    let autoScrollSpeed = 50;
    const AUTO_SCROLL_SPEED_STORAGE_KEY = 'mangadock-reader-auto-scroll-speed';
    const autoScrollSpeedSlider = { value: '50' };
    const autoScrollSpeedValue = { textContent: '50' };
    const window = { localStorage: storage };
    ${functionSource('syncAutoScrollSpeedUi')}
    ${functionSource('restoreAutoScrollSpeed')}
    ${functionSource('handleAutoScrollSpeedChange')}
    return {
        restoreAutoScrollSpeed,
        handleAutoScrollSpeedChange,
        state: () => ({ speed: autoScrollSpeed, slider: autoScrollSpeedSlider.value, label: autoScrollSpeedValue.textContent })
    };
`);

const saved = new Map([['mangadock-reader-auto-scroll-speed', '85']]);
const storage = {
    getItem: key => saved.get(key) ?? null,
    setItem: (key, value) => saved.set(key, value),
};
const reader = createReaderSpeed(storage);
reader.restoreAutoScrollSpeed();
assert.deepEqual(reader.state(), { speed: 85, slider: '85', label: '85' });
reader.handleAutoScrollSpeedChange({ target: { value: '100' } });
assert.equal(saved.get('mangadock-reader-auto-scroll-speed'), '100');
assert.deepEqual(createReaderSpeed(storage).state(), { speed: 50, slider: '50', label: '50' });
const reopened = createReaderSpeed(storage);
reopened.restoreAutoScrollSpeed();
assert.deepEqual(reopened.state(), { speed: 100, slider: '100', label: '100' });

saved.set('mangadock-reader-auto-scroll-speed', '999');
const invalid = createReaderSpeed(storage);
invalid.restoreAutoScrollSpeed();
assert.equal(invalid.state().speed, 50);
invalid.handleAutoScrollSpeedChange({ target: { value: '11' } });
assert.equal(invalid.state().speed, 50);

const unavailable = createReaderSpeed({
    getItem: () => { throw new Error('storage unavailable'); },
    setItem: () => { throw new Error('storage unavailable'); },
});
unavailable.restoreAutoScrollSpeed();
unavailable.handleAutoScrollSpeedChange({ target: { value: '65' } });
assert.deepEqual(unavailable.state(), { speed: 65, slider: '65', label: '65' });

console.log('Reader auto-scroll speed persists per browser');
