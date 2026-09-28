const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

const source = fs.readFileSync(path.join(__dirname, '../templates/reader.html'), 'utf8');
function functionSource(name) {
    const start = source.indexOf(`function ${name}(`);
    assert.ok(start >= 0, `${name} is present`);
    const bodyStart = source.indexOf('{', start);
    let depth = 0;
    for (let index = bodyStart; index < source.length; index++) {
        if (source[index] === '{') depth++;
        if (source[index] === '}' && --depth === 0) return source.slice(start, index + 1);
    }
    throw new Error(`Unclosed function: ${name}`);
}

const run = new Function('assert', `
    let bottom = 105;
    let reports = 0;
    let currentChapter = 0;
    let currentChapterReady = true;
    let anilistEnabled = true;
    let isDraggingProgressSlider = false;
    let progressUiFrameId = null;
    let savedScrollPosition = 0;
    const autoSyncedChapters = new Set();
    const continuousSections = [];
    const pageContent = {getBoundingClientRect: () => ({bottom})};
    const chapters = [{}, {}];
    const window = {innerHeight: 100, scrollY: 95, requestAnimationFrame: callback => {callback(); return 1;}};
    const reportCompletedChapter = () => {reports++;};
    const syncContinuousReading = () => {};
    const syncProgressUi = () => {};
    const loadChapter = () => {};
    ${functionSource('completeCurrentChapterAtEnd')}
    ${functionSource('queueProgressUiSync')}
    ${functionSource('goToNextChapter')}

    queueProgressUiSync();
    assert.equal(reports, 0, '95% scroll must not complete before the content ends');
    progressUiFrameId = null;
    bottom = 100;
    currentChapterReady = false;
    queueProgressUiSync();
    assert.equal(reports, 0, 'the final page must be ready');
    progressUiFrameId = null;
    currentChapterReady = true;
    queueProgressUiSync();
    assert.equal(reports, 1, 'reaching the loaded final page completes the chapter');
    progressUiFrameId = null;
    queueProgressUiSync();
    assert.equal(reports, 1, 'completion is deduplicated');
    currentChapter = 1;
    bottom = 105;
    goToNextChapter();
    assert.equal(reports, 1, 'next-chapter navigation cannot complete early');
`);
run(assert);
console.log('AniList completion requires the loaded chapter end');
