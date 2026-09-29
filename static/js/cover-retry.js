/* 封面加载重试（2026-09-20）：带 data-fallback 的 img 加载失败时，
   先重试原地址两次（1.2s / 2.8s）再回落占位图，吸收网络/隧道瞬时失败。
   加载成功后重置计数，保证同一 img 后续失败仍能重试（P2 修复）。 */
(function () {
    'use strict';
    var DELAYS = [1200, 2800];
    var pendingTimers = new WeakMap();

    function cancelPendingRetry(img) {
        var timer = pendingTimers.get(img);
        if (timer !== undefined) {
            clearTimeout(timer);
            pendingTimers.delete(img);
        }
    }

    document.addEventListener('load', function (e) {
        var img = e.target;
        if (img && img.tagName === 'IMG') {
            cancelPendingRetry(img);
            img.removeAttribute('data-err-n');
            img.removeAttribute('data-orig-src');
        }
    }, true);
    document.addEventListener('error', function (e) {
        var img = e.target;
        if (!img || img.tagName !== 'IMG' || !img.hasAttribute('data-fallback')) return;
        cancelPendingRetry(img);
        var src = img.getAttribute('src') || '';
        if (src.indexOf('cover.png') !== -1 || src.indexOf('default-comic-cover.jpg') !== -1) return;
        var orig = img.getAttribute('data-orig-src');
        var retryPrefix = orig && orig + (orig.indexOf('?') > -1 ? '&' : '?') + 'retry=';
        if (!orig || (src !== orig && !src.startsWith(retryPrefix))) {
            orig = src;
            img.setAttribute('data-orig-src', orig);
            img.removeAttribute('data-err-n');
        }
        var n = parseInt(img.getAttribute('data-err-n') || '0', 10);
        if (n >= DELAYS.length) {
            img.src = img.getAttribute('data-fallback');
            return;
        }
        img.setAttribute('data-err-n', String(n + 1));
        var timer = setTimeout(function () {
            pendingTimers.delete(img);
            if (img.getAttribute('src') !== src) {
                img.removeAttribute('data-err-n');
                img.removeAttribute('data-orig-src');
                return;
            }
            img.src = orig + (orig.indexOf('?') > -1 ? '&' : '?') + 'retry=' + n + Date.now();
        }, DELAYS[n]);
        pendingTimers.set(img, timer);
    }, true);
})();
