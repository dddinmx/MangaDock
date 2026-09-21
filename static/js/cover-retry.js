/* 封面加载重试（2026-09-20）：带 data-fallback 的 img 加载失败时，
   先重试原地址两次（1.2s / 2.8s）再回落占位图，吸收网络/隧道瞬时失败。
   加载成功后重置计数，保证同一 img 后续失败仍能重试（P2 修复）。 */
(function () {
    'use strict';
    var DELAYS = [1200, 2800];
    document.addEventListener('load', function (e) {
        var img = e.target;
        if (img && img.tagName === 'IMG' && img.hasAttribute('data-err-n')) {
            img.removeAttribute('data-err-n');
            img.removeAttribute('data-orig-src');
        }
    }, true);
    document.addEventListener('error', function (e) {
        var img = e.target;
        if (!img || img.tagName !== 'IMG' || !img.hasAttribute('data-fallback')) return;
        var src = img.getAttribute('src') || '';
        if (src.indexOf('cover.png') !== -1) return;
        var n = parseInt(img.getAttribute('data-err-n') || '0', 10);
        if (n >= DELAYS.length) {
            img.src = img.getAttribute('data-fallback');
            return;
        }
        img.setAttribute('data-err-n', String(n + 1));
        setTimeout(function () {
            var cur = img.getAttribute('src') || '';
            if (cur.indexOf('cover.png') !== -1) return;
            if (!img.hasAttribute('data-orig-src')) img.setAttribute('data-orig-src', cur);
            var orig = img.getAttribute('data-orig-src');
            img.src = orig + (orig.indexOf('?') > -1 ? '&' : '?') + 'retry=' + n + Date.now();
        }, DELAYS[n]);
    }, true);
})();
