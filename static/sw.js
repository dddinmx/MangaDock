const STATIC_CACHE_NAME = 'mangadock-static-v9';
const PAGE_CACHE_PREFIX = 'mangadock-pages-';

// 2026-09-20 code review P2：不再缓存任何 HTML 导航响应。
// 本应用所有页面都由服务端按「当前登录用户」渲染（顶栏用户名、私有书架 /
// 历史 / 统计 / 任务列表），旧实现把它们按 origin 共享缓存，并在 1200ms 超时
// 或网络异常时直接吐出缓存 —— 会话静默过期或同一浏览器换号后，可看到上一账号
// 的页面内容（跨会话信息泄露）；缓存下来的登录页还会带上失效的 CSRF token。
// 现在导航请求一律直连网络（见文件末尾 fetch 监听器），SW 只负责 /static/。
const PRECACHE_URLS = [
  '/static/favicon.png',
  '/static/logo-192.png',
  '/static/css/app.generated.css',
  '/static/css/font-awesome.min.css',
];

// /static/ 里也不能缓存的路径：漫画图片体积大且属用户私有内容，
// 接口类路径同样只走网络。
const UNCACHEABLE_PREFIXES = [
  '/api/',
  '/novel',
  '/comic/',
  '/reader/',
  '/save_progress',
  '/get_progress/',
  '/task_status/',
  '/delete_task/',
  '/cancel_task/',
  '/static/comic/',
  '/static/cover/',
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

async function clearPageCache() {
  // 页面缓存已整体停用；这里只负责清掉历史版本遗留的 page cache，
  // 兼容旧客户端（login.html）发来的 CLEAR_PAGE_CACHE 指令。
  const cacheNames = await caches.keys();
  await Promise.all(
    cacheNames
      .filter((cacheName) => cacheName.startsWith(PAGE_CACHE_PREFIX))
      .map((cacheName) => caches.delete(cacheName))
  );
}

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE_NAME)
      .then((cache) => cache.addAll(PRECACHE_URLS))
      .catch((error) => {
        // addAll 是原子操作：任一预缓存资源 404（例如 css 改名）都会整体 reject。
        // 不能让安装因此失败，否则 PWA 彻底不生效；记录后照常激活。
        console.warn('[sw] 预缓存资源失败，跳过但继续激活:', error);
      })
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  // 只保留当前静态缓存；page cache 等历史缓存全部清掉。
  const cacheWhitelist = [STATIC_CACHE_NAME];

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

  // 导航请求（含 HTML 页面）不拦截：直连网络，避免跨会话/跨账号的缓存泄露。
  if (shouldHandleStatic(event.request, url)) {
    event.respondWith(handleStaticRequest(event.request));
  }
});
