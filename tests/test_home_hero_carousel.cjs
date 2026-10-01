const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/js/home-hero-carousel.js'), 'utf8');
const slides = [0, 1, 2].map((index) => ({
    name: `漫画 ${index + 1}`,
    group: '最近观看',
    format: 2,
    chapters: index + 3,
    description: `简介 ${index + 1}`,
    hasProgress: true,
    lastChapter: index,
    lastPage: 2,
    adult: index === 2,
    cover: `/cover/${index}.jpg`,
    art: `/hero/${index}.jpg`,
    artMobile: `/hero-mobile/${index}.jpg`,
    banner: index === 2 ? '/banner/2.jpg' : '',
    detail: `/comic/${index}`,
    reader: `/reader/${index}`,
}));

function element() {
    const classes = new Set();
    const listeners = new Map();
    return {
        classList: {
            add: (name) => classes.add(name),
            remove: (...names) => names.forEach((name) => classes.delete(name)),
            toggle(name, on) { if (on) classes.add(name); else classes.delete(name); },
            contains: (name) => classes.has(name),
        },
        dataset: {},
        style: {},
        textContent: '',
        hidden: false,
        addEventListener: (name, callback) => listeners.set(name, callback),
        dispatchEvent: () => {},
        removeEventListener: (name) => listeners.delete(name),
        setAttribute(name, value) { this[name] = value; },
        removeAttribute(name) { delete this[name]; },
        getBoundingClientRect: () => ({left: 0}),
        listeners,
    };
}

const selectors = [
    '[data-hero-title]', '[data-hero-title-text]', '[data-hero-format]', '[data-hero-adult]', '[data-hero-group]',
    '[data-hero-chapters]', '[data-hero-progress]', '[data-hero-progress-sep]',
    '#hero-comic-description', '[data-hero-description-toggle]', '.md-home-detail-link',
    '[data-hero-reader-link]', '[data-hero-reader-label]', '.md-hero-art-current',
    '.md-hero-art-next', '.md-hero-art', '.md-hero-copy', '.md-hero-indicators',
    '.md-hero-media-img', '.md-hero-media source', '.md-hero-banner-art img',
    '.md-hero-swipe-zone', '.md-home-rail__track',
    '[data-hero-direction="-1"]', '[data-hero-direction="1"]',
];
const nodes = new Map(selectors.map((selector) => [selector, element()]));
const artNodes = [nodes.get('.md-hero-art-current'), nodes.get('.md-hero-art-next')];
artNodes[0].classList.add('md-hero-art-current');
artNodes[1].classList.add('md-hero-art-next');
nodes.set('.md-hero-desc', nodes.get('#hero-comic-description'));
nodes.get('#hero-comic-description').scrollHeight = 100;
nodes.get('#hero-comic-description').clientHeight = 50;
nodes.get('[data-hero-description-toggle]').hidden = true;
const copy = nodes.get('.md-hero-copy');
copy.querySelector = (selector) => nodes.get(selector);
copy.parentElement = {appendChild: (child) => { copy.nextCopy = child; }};
copy.cloneNode = () => {
    const clone = element();
    const children = new Map([...nodes].map(([selector]) => [selector, element()]));
    children.set('.md-hero-desc', children.get('#hero-comic-description'));
    children.get('#hero-comic-description').scrollHeight = 100;
    children.get('#hero-comic-description').clientHeight = 50;
    clone.querySelector = (selector) => children.get(selector);
    return clone;
};
const dots = slides.map(element);
const cards = slides.map(element);
const track = nodes.get('.md-home-rail__track');
track.clientWidth = 390;
track.scrollWidth = 660;
track.scrollLeft = 40;
track.scrollTo = () => { throw new Error('Hero switching must not move the recent rail'); };
cards.forEach((card, index) => {
    card.clientWidth = 104;
    card.getBoundingClientRect = () => ({left: index * 116});
});
const hero = element();
hero.querySelector = (selector) => {
    if (selector === '.md-hero-art-current' || selector === '.md-hero-art-next') {
        return artNodes.find((node) => node.classList.contains(selector.slice(1)));
    }
    return nodes.get(selector);
};
hero.querySelectorAll = (selector) => selector === '.md-hero-indicator' ? dots : cards;

function replaceArt(node) {
    const index = artNodes.indexOf(this);
    assert.notEqual(index, -1);
    artNodes[index] = node;
}
artNodes.forEach((node) => { node.replaceWith = replaceArt; });
const pendingImages = [];
const imageRequests = [];
const settlementDurations = [];
class LoadedImage {
    constructor() {
        Object.assign(this, element());
        Object.defineProperty(this, 'className', {set: (value) => {
            this.classList.remove('md-hero-art-current', 'md-hero-art-next');
            value.split(' ').forEach((name) => this.classList.add(name));
        }});
        this.replaceWith = replaceArt;
    }
    set src(value) {
        imageRequests.push(value);
        this._src = value;
        if (value === '/hero-mobile/1.jpg') pendingImages.push(this);
        else this.onload();
    }
    get src() { return this._src; }
}

vm.runInNewContext(source, {
    document: {
        getElementById: () => ({textContent: JSON.stringify(slides)}),
        querySelector: () => hero,
    },
    Image: LoadedImage,
    Event: class { constructor(type) { this.type = type; } },
    window: {
        matchMedia: (query) => ({matches: query.includes('max-width')}),
        requestAnimationFrame: (callback) => callback(),
        cancelAnimationFrame: () => {},
        setTimeout: (callback, delay) => {
            if (hero.classList.contains('is-sliding') && delay >= 200) {
                const duration = delay - 80;
                assert.equal(hero.querySelector('.md-hero-art-next').style.transition,
                    `transform ${duration}ms cubic-bezier(0.22, 1, 0.36, 1)`,
                    'Completion timer must match the actual CSS duration');
                settlementDurations.push(duration);
            }
            callback();
        },
        innerWidth: 390,
    },
});

async function check() {
    const zone = nodes.get('.md-hero-swipe-zone');
    zone.listeners.get('touchstart')({changedTouches: [{clientX: 200, clientY: 100}]});
    zone.listeners.get('touchend')({changedTouches: [{clientX: 120, clientY: 105}]});
    await Promise.resolve();
    assert.equal(nodes.get('[data-hero-title-text]').textContent, '');
    pendingImages.shift().onload();
    await Promise.resolve();
    assert.equal(nodes.get('[data-hero-title-text]').textContent, '漫画 2');
    assert.equal(nodes.get('.md-home-detail-link').href, '/comic/1');
    assert.equal(nodes.get('[data-hero-reader-link]').href, '/reader/1');
    assert.equal(hero.querySelector('.md-hero-art-current').src, '/hero-mobile/1.jpg');
    assert.equal(hero.querySelector('.md-hero-art-current').srcset, undefined);
    assert.equal(nodes.get('[data-hero-description-toggle]').hidden, false);
    assert.equal(dots[1].classList.contains('is-active'), true);
    assert.equal(cards[1].classList.contains('is-current'), true);
    assert.equal(track.scrollLeft, 40);

    zone.listeners.get('touchstart')({changedTouches: [{clientX: 100, clientY: 100}]});
    zone.listeners.get('touchend')({changedTouches: [{clientX: 170, clientY: 104}]});
    await Promise.resolve();
    assert.equal(nodes.get('[data-hero-title-text]').textContent, '漫画 1');
    assert.equal(track.scrollLeft, 40);

    dots[2].listeners.get('click')();
    await Promise.resolve();
    assert.equal(nodes.get('[data-hero-title-text]').textContent, '漫画 3');
    assert.equal(nodes.get('[data-hero-adult]').hidden, false);
    assert.equal(nodes.get('.md-hero-banner-art img').src, undefined);

    zone.listeners.get('touchstart')({changedTouches: [{identifier: 1, clientX: 280, clientY: 100}]});
    zone.listeners.get('touchmove')({changedTouches: [{identifier: 1, clientX: 160, clientY: 104}]});
    assert.equal(hero.classList.contains('is-sliding'), true);
    assert.equal(copy.nextCopy.querySelector('[data-hero-title-text]').textContent, '漫画 1');
    assert.equal(copy.nextCopy.querySelector('.md-hero-desc').textContent, '简介 1');
    assert.equal(copy.nextCopy.querySelector('[data-hero-description-toggle]').hidden, false);
    assert.equal(nodes.get('[data-hero-description-toggle]').hidden, false);
    assert.equal(hero.querySelector('.md-hero-art-next').src, '/hero-mobile/0.jpg');
    assert.equal(copy.style.transform.includes('translate3d(-'), true);
    zone.listeners.get('touchend')({changedTouches: [{identifier: 1, clientX: 160, clientY: 104}]});
    assert.equal(nodes.get('[data-hero-title-text]').textContent, '漫画 1');
    assert.equal(hero.classList.contains('is-sliding'), false);

    zone.listeners.get('touchstart')({changedTouches: [{identifier: 2, clientX: 240, clientY: 100}]});
    zone.listeners.get('touchmove')({changedTouches: [{identifier: 2, clientX: 220, clientY: 100}]});
    zone.listeners.get('touchend')({changedTouches: [{identifier: 2, clientX: 220, clientY: 100}]});
    assert.equal(nodes.get('[data-hero-title-text]').textContent, '漫画 1');
    assert.equal(hero.classList.contains('is-sliding'), false);
    nodes.get('[data-hero-direction="1"]').listeners.get('click')();
    await Promise.resolve();
    assert.equal(nodes.get('[data-hero-title-text]').textContent, '漫画 2');
    nodes.get('[data-hero-direction="-1"]').listeners.get('click')();
    await Promise.resolve();
    assert.equal(nodes.get('[data-hero-title-text]').textContent, '漫画 1');
    assert.equal(track.scrollLeft, 40);
    assert(settlementDurations.includes(260));
    assert(settlementDurations.some((duration) => duration < 260));
    console.log('Home hero swipe updates controls without moving the recent rail');
}
check().catch((error) => { console.error(error); process.exitCode = 1; });
