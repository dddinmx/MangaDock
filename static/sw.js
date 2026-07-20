const STATIC_CACHE_NAME = 'mangadock-static-v5';
const PAGE_CACHE_NAME = 'mangadock-pages-v6';
const NAVIGATION_TIMEOUT_MS = 1200;

const PRECACHE_URLS = [
  '/static/icon.png',
  '/static/favicon.ico',
  '/static/css/app.generated.css',
  '/static/css/font-awesome.min.css',
];

const UNCACHEABLE_PREFIXES = [
  '/api/',
  '/comic/',
  '/reader/',
  '/save_progress',
  '/get_progress/',
  '/task_status/',
  '/delete_task/',
  '/cancel_task/',
  '/static/comic/',
  '/settings',
  '/users',
  '/progress/',
];

function isSameOrigin(url) {
  return url.origin === self.location.origin;
}

function isUncacheablePath(pathname) {
  return UNCACHEABLE_PREFIXES.some((prefix) => pathname.startsWith(prefix));
}

function shouldHandleNavigation(request, url) {
  if (request.method !== 'GET') {
    return false;
  }

  return isSameOrigin(url) && request.mode === 'navigate' && !isUncacheablePath(url.pathname);
}

function shouldHandleStatic(request, url) {
  if (request.method !== 'GET') {
    return false;
  }

  if (!isSameOrigin(url) || request.mode === 'navigate' || isUncacheablePath(url.pathname)) {
    return false;
  }

  return url.pathname.startsWith('/static/');
}

async function cacheStaticResponse(request, response) {
  if (!response || response.status !== 200 || response.type !== 'basic') {
    return response;
  }

  const cache = await caches.open(STATIC_CACHE_NAME);
  cache.put(request, response.clone());
  return response;
}

async function handleStaticRequest(request) {
  const cachedResponse = await caches.match(request);
  const networkFetch = fetch(request)
    .then((response) => cacheStaticResponse(request, response))
    .catch(() => cachedResponse);

  return cachedResponse || networkFetch;
}

async function handleNavigationRequest(request) {
  const cache = await caches.open(PAGE_CACHE_NAME);
  const cachedResponse = await cache.match(request);
  const networkFetch = fetch(request)
    .then(async (response) => {
      const contentType = response.headers.get('content-type') || '';
      if (response.status === 200 && contentType.includes('text/html')) {
        await cache.put(request, response.clone());
      }
      return response;
    });

  if (!cachedResponse) {
    return networkFetch;
  }

  const timeoutFallback = new Promise((resolve) => {
    setTimeout(() => resolve(cachedResponse), NAVIGATION_TIMEOUT_MS);
  });

  try {
    return await Promise.race([networkFetch, timeoutFallback]);
  } catch (error) {
    return cachedResponse;
  }
}

async function clearPageCache() {
  await caches.delete(PAGE_CACHE_NAME);
}

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE_NAME)
      .then((cache) => cache.addAll(PRECACHE_URLS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  const cacheWhitelist = [STATIC_CACHE_NAME, PAGE_CACHE_NAME];

  event.waitUntil(
    caches.keys()
      .then((cacheNames) => Promise.all(
        cacheNames.map((cacheName) => {
          if (!cacheWhitelist.includes(cacheName)) {
            return caches.delete(cacheName);
          }
          return undefined;
        })
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'CLEAR_PAGE_CACHE') {
    event.waitUntil(clearPageCache());
  }
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  if (shouldHandleNavigation(event.request, url)) {
    event.respondWith(handleNavigationRequest(event.request));
    return;
  }

  if (shouldHandleStatic(event.request, url)) {
    event.respondWith(handleStaticRequest(event.request));
  }
});
