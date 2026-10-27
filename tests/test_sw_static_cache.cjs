const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const listeners = {};
const stored = new Map();
let networkRequests = 0;
const cache = {
    match: async (request) => stored.get(request.url),
    put: async (request, response) => { stored.set(request.url, response); },
};
const context = {
    URL,
    self: {
        location: {origin: 'https://mangadock.test'},
        addEventListener: (name, listener) => { listeners[name] = listener; },
    },
    caches: {open: async () => cache},
    fetch: async () => {
        networkRequests += 1;
        return {status: 200, type: 'basic', clone() { return this; }};
    },
};
vm.runInNewContext(
    fs.readFileSync(path.join(__dirname, '../static/sw.js'), 'utf8'),
    context,
);

async function requestStatic() {
    const pending = [];
    let response;
    listeners.fetch({
        request: {
            method: 'GET', mode: 'same-origin',
            url: 'https://mangadock.test/static/css/md-theme.css?v=123',
        },
        respondWith: (promise) => { response = promise; },
        waitUntil: (promise) => { pending.push(promise); },
    });
    await response;
    await Promise.all(pending);
}

(async () => {
    await requestStatic();
    await requestStatic();
    assert.equal(networkRequests, 1, 'versioned static files should use cache after first load');
    assert.equal(stored.size, 1);
})().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
