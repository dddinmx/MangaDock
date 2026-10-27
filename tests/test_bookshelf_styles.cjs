const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');

const theme = fs.readFileSync(path.join(__dirname, '../static/css/md-theme.css'), 'utf8');
const polish = fs.readFileSync(path.join(__dirname, '../static/css/ui-polish.css'), 'utf8');

test('bookshelf theme retains its shared color and sizing variables', () => {
    const root = theme.match(/:root\s*\{([^}]+)\}/);
    assert.ok(root, 'the shared :root theme rule must survive feature cleanup');
    for (const variable of ['--md-bg', '--md-text', '--md-brand', '--md-max']) {
        assert.ok(root[1].includes(variable + ':'), variable);
    }
});

test('comic cards retain hover, keyboard focus and book decoration', () => {
    for (const selector of [
        '.md-cover-card:hover .md-book',
        '.md-cover-card:focus-visible .md-book',
        '.md-book::before',
        '.md-book::after',
    ]) assert.ok(theme.includes(selector), selector);
});

test('comic shared styles retain light mode and renamed group controls', () => {
    assert.ok(theme.includes('html:not(.theme-dark)'));
    assert.ok(theme.includes('.md-comic-select-item > input:checked + .md-comic-select-card'));
    assert.ok(polish.includes('.md-comic-groups-page'));
    assert.ok(!theme.includes('md-novel-') && !polish.includes('md-novel-'));
});
