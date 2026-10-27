const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

// This DOM intentionally has no native inert or lazy-loading implementation.
const requests = [], frames = [], timers = new Map();
let timerId = 0, decodes = 0;
class Element {
    constructor() {
        this.attributes = new Map(); this.listeners = new Map(); this.children = [];
        this.nodes = new Map(); this.style = {setProperty() {}}; this.dataset = {};
        this.clientWidth = 390; this.clientHeight = 480; this.scrollHeight = 500;
        this.scrollLeft = 0;
        const classes = new Set();
        this.classList = {
            add: name => classes.add(name), contains: name => classes.has(name),
            toggle(name, value) {if (value) classes.add(name); else classes.delete(name);},
        };
    }
    setAttribute(k, v) {this.attributes.set(k, String(v));}
    getAttribute(k) {return this.attributes.get(k) || null;}
    removeAttribute(k) {this.attributes.delete(k);}
    toggleAttribute(k, on) {if (on) this.setAttribute(k, ''); else this.removeAttribute(k);}
    addEventListener(k, fn) {this.listeners.set(k, fn);}
    dispatchEvent() {}
    appendChild(n) {this.children.push(n);}
    querySelector(k) {if (!this.nodes.has(k)) this.nodes.set(k, new Element()); return this.nodes.get(k);}
    querySelectorAll() {return [];}
    cloneNode() {return new Element();}
    scrollTo(options) {this.scrollLeft = options.left;}
}
class Cover extends Element {
    constructor() {super(); this.complete = false; this.naturalWidth = 0;}
    get src() {return this.getAttribute('src') || '';}
    set src(url) {this.setAttribute('src', url); requests.push(url);}
    decode() {
        decodes++; this.decodeCalls = (this.decodeCalls || 0) + 1;
        if (this.failNextDecode) {this.failNextDecode = false; return Promise.reject(Error('Temporary decoder failure'));}
        return Promise.resolve();
    }
}
const slides = Array.from({length: 18}, (_, i) => ({
    name: `漫画${i}`, group: '默认分组', format: 2, chapters: 10,
    description: '简介', hasProgress: true, lastChapter: 2, lastPage: 0,
    cover: `/cover/${i}.jpg`, artMobile: `/full-quality/${i}.jpg`,
    detail: `/comic/${i}`, reader: `/reader/${i}`,
}));
const original = new Cover(); original.src = slides[0].artMobile;
const inner = new Element(), hero = new Element();
hero.nodes.set('.md-hero-inner', inner);
hero.nodes.set('.md-hero-art-current', original);
const dots = slides.map(() => new Element()), cards = slides.map(() => new Element());
hero.querySelectorAll = selector => selector.includes('indicator') ? dots : cards;
const context = {
    navigator: {userAgent: process.env.MD_TEST_USER_AGENT || 'iPhone OS 15_8', platform: 'iPhone', maxTouchPoints: 5},
    Image: Cover, Event: class {},
    document: {
        querySelector: () => hero,
        getElementById: () => ({textContent: JSON.stringify(slides)}),
        createElement: () => new Element(),
    },
    window: {
        matchMedia: q => ({matches: !q.includes('reduce')}),
        addEventListener() {}, requestAnimationFrame: fn => frames.push(fn),
        setTimeout: fn => {timers.set(++timerId, fn); return timerId;},
        clearTimeout: id => timers.delete(id),
    },
};
vm.runInNewContext(fs.readFileSync('static/js/home-hero-native.js', 'utf8'), context);
const viewport = inner.children[0], panels = viewport.children;
assert.equal(hero.dataset.nativeCarousel, 'true', 'All mobile systems initialize the same native carousel');
assert.equal(hero.dataset.legacyCarousel, undefined, 'No version-specific carousel is selected');
assert.equal(panels.length, 18);
assert.equal(panels.filter(p => p.children[0].children[0].src).length, 1,
    'Without lazy-loading support, building the carousel still assigns only its first cover');
async function check() {
frames.shift()();
assert.equal(panels.filter(p => p.children[0].children[0].src).length, 4,
    'Initial preloading is bounded to the first four full-quality covers');
assert.equal(panels.filter(p => !p.attributes.has('inert')).length, 1,
    'One active panel is identifiable even without the inert property');
assert.equal(panels[0].getAttribute('aria-hidden'), 'false');
assert.ok(panels[1].attributes.has('inert'));
assert.equal(panels[17].children[0].children[0].src, '');
for (let i = 0; i < 4; i++) {
    const image = panels[i].children[0].children[0];
    image.complete = true; image.naturalWidth = 1100;
    image.listeners.get('load')();
}
await Promise.resolve(); await Promise.resolve();
assert.equal(decodes, 2, 'Only current and adjacent covers are decoded initially');
const surfacesBefore = panels.map(p => p.classList.contains('is-near'));
viewport.scrollLeft = 240;
viewport.listeners.get('scroll')();
assert.notDeepEqual(panels.map(p => p.classList.contains('is-near')), surfacesBefore,
    'Approaching a new page prepares its next blurred surface before settling');
assert.ok(panels[2].classList.contains('is-near'));
await Promise.resolve(); await Promise.resolve();
assert.equal(panels[2].children[0].children[0].decodeCalls, 1,
    'A previously downloaded ahead cover is decoded when it becomes adjacent');
viewport.listeners.get('scrollend')();
assert.ok(panels[2].classList.contains('is-near'), 'The next surface is prepared after settling');

dots[10].listeners.get('click')();
assert.equal(viewport.scrollLeft, 3900);
viewport.listeners.get('scroll')();
viewport.listeners.get('scrollend')();
assert.equal(panels.filter(p => !p.attributes.has('inert')).length, 1);
assert.equal(panels[10].getAttribute('aria-hidden'), 'false');
assert.ok(panels[0].attributes.has('inert'));
for (const i of [8, 9, 10, 11, 12, 13]) {
    assert.equal(panels[i].children[0].children[0].src, slides[i].artMobile);
}
assert.equal(panels[0].children[0].children[0].src, slides[0].artMobile,
    'Returning to an earlier page never clears an already loaded source');
assert.equal(panels[17].children[0].children[0].src, '', 'Far-off covers remain unloaded');
const finishLoads = async () => {
    for (const panel of panels) {
        const image = panel.children[0].children[0];
        if (image.src && !image.complete) {
            image.complete = true; image.naturalWidth = 1100;
            image.listeners.get('load')();
        }
    }
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
};
await finishLoads();
// Do not fire scrollend or debounce timers: repeated touch swipes can keep scrolling busy.
panels[2].children[0].children[0].failNextDecode = true;
for (let index = 0; index < panels.length; index++) {
    viewport.scrollLeft = index * 390;
    viewport.listeners.get('scroll')();
    await finishLoads();
    for (const target of [index - 1, index, index + 1].filter(n => n >= 0 && n < panels.length)) {
        assert.ok(panels[target].classList.contains('is-near'), 'Visible and adjacent blurred surfaces stay ready during continuous swipes');
        const image = panels[target].children[0].children[0];
        assert.equal(image.src, slides[target].artMobile);
        assert.ok(image.decodeCalls > 0, 'Every upcoming cover is decoded despite a cached load promise');
    }
    assert.ok(panels.filter(panel => panel.classList.contains('is-near')).length <= 3,
        'Continuous swipes keep at most three promoted surfaces');
}
assert.ok(panels[2].children[0].children[0].decodeCalls >= 3,
    'A temporary decode failure is retried as the page approaches, rather than cached as ready');
const sourceRequests = requests.length;
const priorDecodes = panels[0].children[0].children[0].decodeCalls;
for (let index = panels.length - 2; index >= 0; index--) {
    viewport.scrollLeft = index * 390;
    viewport.listeners.get('scroll')();
    await finishLoads();
    assert.ok(panels[index].classList.contains('is-near'));
}
assert.equal(requests.length, sourceRequests, 'Revisiting keeps loaded image URLs without extra requests');
assert.ok(panels[0].children[0].children[0].decodeCalls > priorDecodes,
    'Returning to an old cover prepares its decoded surface again');
viewport.listeners.get('scrollend')();
assert.equal(panels[0].getAttribute('aria-hidden'), 'false');
console.log('Continuous forward/back swipes keep three surfaces ready, decode cached covers and preserve image quality');
}
check().catch(error => {console.error(error); process.exitCode = 1;});
