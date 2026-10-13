/* Durable latest snapshots with bounded retries. Normal reader layout is unchanged. */
(function (root) {
    'use strict';
    root.MangaDockProgressSync = function (options) {
        var pending = [], inFlight = false, failures = 0, timer = null, pausedUntil = 0, notice = null;
        var delays = [5000, 30000, 120000];
        try { pending = JSON.parse(localStorage.getItem(options.key) || '[]'); if (!Array.isArray(pending)) pending = []; pending = pending.slice(-4); } catch (_) { pending = []; }
        function persist() { try { if (pending.length) localStorage.setItem(options.key, JSON.stringify(pending)); else localStorage.removeItem(options.key); } catch (_) {} }
        function indicate(failed) {
            if (!failed) { if (notice) notice.hidden = true; return; }
            if (!notice) {
                notice = document.createElement('div'); notice.setAttribute('role', 'status'); notice.setAttribute('aria-live', 'polite');
                notice.id = 'progress-sync-status';
                notice.style.cssText = 'position:fixed;right:16px;bottom:80px;z-index:1000;max-width:280px;padding:8px 12px;background:#262630;color:#fff;border-radius:8px;font-size:13px;cursor:pointer';
                notice.addEventListener('click', resume); document.body.appendChild(notice);
            }
            notice.hidden = false; notice.textContent = '进度尚未同步，已保留在此设备。点此重试';
        }
        function schedule(delay) {
            clearTimeout(timer);
            if (failures <= delays.length && !document.hidden) timer = setTimeout(function () { flush(false); }, delay || delays[failures - 1] || delays[0]);
        }
        async function flush(keepalive) {
            if (inFlight || !pending.length || (Date.now() < pausedUntil && !keepalive)) return false;
            if (failures > delays.length && !keepalive) return false;
            inFlight = true;
            var snapshot = pending[0];
            try {
                var response = await fetch(options.url, { method: 'POST', credentials: 'same-origin', keepalive: !!keepalive, headers: {'Content-Type': 'application/json', 'X-CSRFToken': options.csrf}, body: JSON.stringify(snapshot) });
                if (!response.ok || response.redirected) { var error = new Error('HTTP ' + response.status); error.status = response.status; error.retryAfter = Math.min(86400, Math.max(0, Number(response.headers.get('Retry-After')) || 0)); throw error; }
                var data = await response.json();
                if (!(data.success === true || data.ok === true || data.status === 'success')) throw new Error('Progress rejected');
                pending = pending.filter(function (item) { return item !== snapshot; });
                persist(); failures = 0; pausedUntil = 0; indicate(false);
                if (options.onSaved) options.onSaved(snapshot);
                return true;
            } catch (error) {
                indicate(true); failures++;
                if (error.status === 401 || error.status === 403) { failures = delays.length + 1; }
                else { var delay = Math.max((error.retryAfter || 0) * 1000, delays[failures - 1] || 900000); pausedUntil = Date.now() + delay; if (!keepalive) schedule(delay); }
                return false;
            } finally {
                inFlight = false;
                if (pending.length && failures === 0 && !keepalive) { clearTimeout(timer); timer = setTimeout(function () { flush(false); }, 250); }
            }
        }
        function resume() { if (document.hidden) return; failures = 0; pausedUntil = 0; clearTimeout(timer); flush(false); }
        function save(snapshot, keepalive) {
            var session = snapshot.reading_session_id || 'progress';
            var previous = pending.find(function (item) { return (item.reading_session_id || 'progress') === session; });
            if (previous) snapshot.reading_time = Math.max(snapshot.reading_time || 0, previous.reading_time || 0);
            pending = pending.filter(function (item) { return item !== previous; }); pending.push(snapshot); pending = pending.slice(-4); persist();
            return flush(keepalive);
        }
        root.addEventListener('online', resume);
        if (pending.length) { indicate(true); timer = setTimeout(function () { flush(false); }, 1000); }
        return {save:save,retry:resume};
    };
})(window);
