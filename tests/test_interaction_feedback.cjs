const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const listeners = new Map();
let animations = 0;
const classes = new Set();
const button = {
    disabled: false,
    closest: () => null,
    getAttribute: () => null,
    classList: {add: (value) => classes.add(value), remove: (value) => classes.delete(value)},
    animate: () => { animations++; return {cancel() {}}; },
};
const document = {hidden: false, addEventListener: (name, handler) => listeners.set(name, handler)};
const windowEvents = new Map();
vm.runInNewContext(fs.readFileSync('static/js/interaction-feedback.js', 'utf8'), {
    document, window: {matchMedia: () => ({matches: false}), addEventListener: (name, handler) => windowEvents.set(name, handler)},
});
const event = {target: {closest: () => button}, button: 0, clientX: 10, clientY: 10};
listeners.get('pointerdown')(event);
assert(classes.has('md-control-pressed'), 'Touch/mouse press must be immediate');
listeners.get('pointermove')({...event, clientX: 30});
assert(!classes.has('md-control-pressed'), 'Scrolling must cancel feedback');
listeners.get('pointerdown')(event);
listeners.get('pointercancel')(event);
assert(!classes.has('md-control-pressed'));
listeners.get('click')(event);
assert.equal(animations, 1);
button.disabled = true;
listeners.get('pointerdown')(event);
listeners.get('click')(event);
assert(!classes.has('md-control-pressed'));
assert.equal(animations, 1, 'Disabled buttons must not animate');
button.disabled = false;
listeners.get('keydown')({...event, key: 'Enter'});
assert(classes.has('md-control-pressed'), 'Keyboard gets the same immediate feedback');
windowEvents.get('pageshow')();
assert(!classes.has('md-control-pressed'), 'Back navigation must clear pressed state');
console.log('Interaction feedback covers press, scroll cancellation, keyboard and page restoration');
