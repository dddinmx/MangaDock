(() => {
    const entryKey = 'mangadock-reader-detail-entry';
    const refreshKey = 'mangadock-reader-detail-refresh';
    let readerEntry = null, refreshController = null, refreshTimer = null;
    const read = key => {
        try { return JSON.parse(sessionStorage.getItem(key) || 'null'); }
        catch (error) { return null; }
    };
    const write = (key, value) => {
        try { sessionStorage.setItem(key, JSON.stringify(value)); }
        catch (error) { /* Native navigation remains available without storage. */ }
    };
    const remove = key => {
        try { sessionStorage.removeItem(key); } catch (error) {}
    };
    if (location.pathname.startsWith('/reader/')) {
        const entry = read(entryKey);
        remove(entryKey);
        if (entry && Date.now() - entry.at < 86400000 && entry.reader === location.href
                && document.referrer && history.length > 1) {
            const previous = new URL(document.referrer);
            previous.hash = '';
            if (previous.href === entry.detail) readerEntry = entry;
        }
    }
    document.addEventListener('click', event => {
        const link = event.target.closest?.('.comic-detail a[href]');
        if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey
                || event.shiftKey || event.altKey || link.hasAttribute('download')
                || (link.target && link.target !== '_self')) return;
        const target = new URL(link.href, location.href);
        if (target.origin !== location.origin || !target.pathname.startsWith('/reader/')) return;
        const detail = new URL(location.href);
        detail.hash = '';
        const back = new URL(target.href);
        back.pathname = back.pathname.replace('/reader/', '/comic/');
        back.searchParams.delete('start_chapter');
        const scroller = document.querySelector('.md-in-app-detail:not([hidden])');
        const ui = {
            scroll: scroller?.scrollTop ?? window.scrollY,
            sort: document.querySelector('#chapter-sort')?.value,
            filter: document.querySelector('#chapter-search')?.value,
            expanded: document.querySelector('#comic-detail-description')?.classList.contains('is-expanded'),
            home: history.state?.mdComicPage?.home ? {
                url: history.state?.mdComicPage?.home,
                scroll: window.scrollY,
                hero: document.querySelector('.md-hero-native')?.scrollLeft
            } : window.mdReaderHomePosition
        };
        write(entryKey, {detail: detail.href, back: back.href, reader: target.href, ui, at: Date.now()});
    }, true);
    window.returnReaderToDetail = target => {
        const url = new URL(target, location.href);
        url.hash = '';
        if (!readerEntry || (readerEntry.detail !== url.href && readerEntry.back !== url.href)) return false;
        write(refreshKey, {detail: readerEntry.detail, ui: readerEntry.ui, at: Date.now()});
        history.back();
        return true;
    };
    async function refreshProgress() {
        const url = location.href;
        const detail = document.querySelector('.comic-detail');
        if (!detail) return;
        refreshController?.abort();
        const controller = new AbortController();
        refreshController = controller;
        const timeout = setTimeout(() => controller.abort(), 15000);
        try {
            const response = await fetch(url, {credentials: 'same-origin', signal: controller.signal});
            if (!response.ok || response.redirected) return;
            const page = new DOMParser().parseFromString(await response.text(), 'text/html');
            if (location.href !== url || !detail.isConnected || controller.signal.aborted) return;
            // Only progress changes: keep the scroll, synopsis, chapter filter and ordering.
            detail.querySelectorAll('[data-detail-progress]').forEach(node => {
                const fresh = page.querySelector(`[data-detail-progress="${node.dataset.detailProgress}"]`);
                if (fresh) node.replaceChildren(...Array.from(fresh.childNodes, child => document.importNode(child, true)));
            });
            const readChapters = new Set(Array.from(page.querySelectorAll('.chapter-item.is-in-reading-progress'), node => node.href));
            detail.querySelectorAll('.chapter-item').forEach(node => {
                node.classList.toggle('is-in-reading-progress', readChapters.has(node.href));
            });
            window.initializeComicDetail?.();
        } catch (error) {
            // Retained content stays usable when the optional refresh fails.
        } finally {
            clearTimeout(timeout);
            if (refreshController === controller) refreshController = null;
        }
    }
    window.addEventListener('pagehide', () => {
        if (readerEntry) write(refreshKey, {detail: readerEntry.detail, ui: readerEntry.ui, at: Date.now()});
        clearTimeout(refreshTimer);
        refreshController?.abort();
    });
    window.addEventListener('pageshow', event => {
        const refresh = read(refreshKey);
        if (!refresh || refresh.detail !== location.href) return;
        remove(refreshKey);
        if (Date.now() - refresh.at >= 120000) return;
        if (!event.persisted && refresh.ui) {
            // iOS can evict BFCache under memory pressure. Restore small UI state too.
            const sort = document.querySelector('#chapter-sort');
            const search = document.querySelector('#chapter-search');
            if (sort && refresh.ui.sort) { sort.value = refresh.ui.sort; window.sortChapters?.(); }
            if (search && refresh.ui.filter) { search.value = refresh.ui.filter; window.searchChapters?.(); }
            if (refresh.ui.expanded) document.querySelector('[data-comic-synopsis-toggle]')?.click();
            window.mdReaderHomePosition = refresh.ui.home;
            window.requestAnimationFrame(() => window.scrollTo(0, refresh.ui.scroll || 0));
        }
        if (event.persisted) {
            // Let the restored frame paint and the reader's keepalive save finish first.
            refreshTimer = setTimeout(refreshProgress, 250);
        }
    });
})();
