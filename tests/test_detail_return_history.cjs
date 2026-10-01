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
        shell: {contains: () => true}, pending: null, active: true, motion: null, ready: true,
        returnByPush, showHome: () => slides++, state: type => ({type}),
        history: {back: () => backs++, pushState: (state, _, url) => {
            assert.equal(state.type, 'home'); assert.equal(url, 'http://localhost/'); pushes++;
        }}
    };
    vm.runInNewContext(source.slice(start, end), context);
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
console.log('Home-origin and reader-origin details both close to home using the correct history path');
