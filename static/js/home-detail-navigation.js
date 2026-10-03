function initializeHomeDetailNavigation(restored = null) {
    const home = restored?.home || document.querySelector('body.md-home-page > .app-main-content');
    if (!home || !window.fetch) return;
    const homeUrl = restored?.homeUrl || location.href;
    const homeTitle = restored?.homeTitle || document.title;
    const reduced = matchMedia('(prefers-reduced-motion: reduce)');
    const mobile = matchMedia('(max-width: 767px)');
    const dock = document.querySelector('.md-mobile-tabbar');
    const shell = document.createElement('section');
    shell.className = 'md-in-app-detail';
    shell.hidden = true;
    shell.tabIndex = -1;
    shell.setAttribute('aria-label', '漫画详情');
    const viewport = document.createElement('div');
    viewport.className = 'md-detail-navigation';
    viewport.hidden = true;
    const homeFrame = document.createElement('div');
    homeFrame.className = 'md-detail-home-frame';
    homeFrame.setAttribute('aria-hidden', 'true');
    viewport.append(homeFrame, shell);
    document.body.appendChild(viewport);
    let currentUrl = null, currentTitle = '', pending = null, active = false;
    let motion = null, sequence = 0, opener = null;
    let warmed = null, warmTimer = null, touching = false, settleTimer = null, homeRequested = false;
    let savedOverflow = '', savedScroll = restored?.homeScroll || 0;
    let returnByPush = !!restored;
    const state = type => ({...history.state, mdComicPage: {type, home: homeUrl,
        returnByPush: type === 'detail' && returnByPush}});
    if (!restored) history.replaceState(state('home'), '', homeUrl);
    let ready = !restored;

    function resetFeedback() {
        opener?.classList.remove('is-loading');
        opener?.removeAttribute('aria-busy');
    }
    async function animate(enter) {
        motion?.cancel();
        motion = null;
        if (!mobile.matches) return;
        // Switch pages immediately; soften the result without horizontal movement.
        viewport.scrollTo({left: enter ? viewport.clientWidth : 0, behavior: 'auto'});
        const target = enter ? shell : home;
        if (reduced.matches || !target.animate) return;
        const fade = target.animate([{opacity: .8}, {opacity: 1}], {
            duration: 140, easing: 'ease-out',
        });
        motion = fade;
        try { await fade.finished; } catch (error) {
            // Leaving the page can cancel the brief fade.
        } finally { fade.cancel(); }
    }
    function finishMotion() {
        motion = null;
    }
    async function showDetail() {
        const ticket = ++sequence;
        if (!active) {
            savedOverflow = document.body.style.overflow;
            savedScroll = scrollY;
        }
        active = true;
        document.body.style.overflow = 'hidden';
        document.body.classList.add('md-in-app-detail-open');
        home.setAttribute('aria-hidden', 'true');
        home.inert = true;
        if (dock) dock.hidden = true;
        shell.hidden = false;
        viewport.hidden = false;
        shell.inert = false;
        viewport.classList.remove('is-preparing');
        viewport.removeAttribute('aria-hidden');
        document.title = currentTitle;
        await animate(true);
        if (ticket !== sequence) return;
        finishMotion();
        if (mobile.matches && viewport.scrollLeft < viewport.clientWidth / 2) {
            requestHome();
            return;
        }
        shell.focus({preventScroll: true});
        resetFeedback();
        window.refreshComicSynopsisLayout?.();
    }
    async function showHome() {
        const ticket = ++sequence;
        homeRequested = false;
        pending?.abort(); pending = null;
        resetFeedback();
        document.title = homeTitle;
        if (!active) return;
        home.style.visibility = '';
        await animate(false);
        if (ticket !== sequence) return;
        if (mobile.matches && viewport.scrollLeft >= viewport.clientWidth / 2) {
            finishMotion();
            // A finger can take over a native scroll and cancel the button's return.
            homeRequested = true;
            history.go(returnByPush ? -1 : 1);
            return;
        }
        shell.hidden = true;
        viewport.hidden = true;
        finishMotion();
        active = false;
        document.body.style.overflow = savedOverflow;
        document.body.classList.remove('md-in-app-detail-open');
        home.removeAttribute('aria-hidden'); home.inert = false;
        if (dock) dock.hidden = false;
        scrollTo(0, savedScroll);
        opener?.focus({preventScroll: true});
        scheduleWarm();
    }
    function requestHome() {
        if (homeRequested) return;
        homeRequested = true;
        if (returnByPush) {
            history.pushState(state('home'), '', homeUrl);
            showHome();
        } else {
            history.back();
        }
    }
    function settleSwipe() {
        clearTimeout(settleTimer);
        if (touching || motion || homeRequested || !active || !mobile.matches || !ready) return;
        if (viewport.scrollLeft < 1) requestHome();
    }
    viewport.addEventListener('touchstart', () => { touching = true; }, {passive: true});
    const releaseTouch = () => {
        touching = false;
        clearTimeout(settleTimer);
        settleTimer = setTimeout(settleSwipe, 140);
    };
    viewport.addEventListener('touchend', releaseTouch, {passive: true});
    viewport.addEventListener('touchcancel', releaseTouch, {passive: true});
    viewport.addEventListener('scroll', () => {
        clearTimeout(settleTimer);
        settleTimer = setTimeout(settleSwipe, 140);
    }, {passive: true});
    viewport.addEventListener('scrollend', settleSwipe);
    window.addEventListener('resize', () => {
        if (!active || motion || touching) return;
        viewport.scrollTo({left: viewport.clientWidth, behavior: 'auto'});
    }, {passive: true});
    async function loadDetail(url, push) {
        pending?.abort();
        const controller = new AbortController();
        pending = controller;
        const timeout = setTimeout(() => controller.abort(), 15000);
        try {
            const prepared = warmed;
            warmed = null;
            if (prepared) {
                if (prepared.url !== url) prepared.controller.abort();
                controller.signal.addEventListener('abort', () => prepared.controller.abort(), {once: true});
            }
            let page;
            if (currentUrl !== url) {
                const usePrepared = prepared?.url === url;
                if (usePrepared) {
                    try { page = await prepared.page; } catch (error) {
                        if (controller.signal.aborted) throw error;
                    }
                }
                if (!page) page = parseDetail(await fetchDetail(url, controller.signal));
                if (pending !== controller) return;
                if (!usePrepared || !prepared.mounted) mountDetail(page);
            } else {
                prepared?.controller.abort();
            }
            if (pending !== controller) return;
            shell.scrollTop = 0;
            currentUrl = url;
            if (page) currentTitle = page.title;
            pending = null;
            if (push) {
                returnByPush = false;
                history.pushState(state('detail'), '', url);
            }
            await showDetail();
        } catch (error) {
            if (pending !== controller) return;
            pending = null; resetFeedback();
            location.assign(url);
        } finally {
            clearTimeout(timeout);
        }
    }
    async function fetchDetail(url, signal) {
        const response = await fetch(url, {credentials: 'same-origin', signal});
        if (!response.ok || response.redirected) throw new Error('Use full navigation');
        return response.text();
    }
    function parseDetail(html) {
        const page = new DOMParser().parseFromString(html, 'text/html');
        const content = page.querySelector('.app-main-content');
        if (!content?.querySelector('.comic-detail')) throw new Error('Missing detail');
        // Shared scripts and event delegation run once.
        content.querySelectorAll('script').forEach(script => script.remove());
        return {content: document.importNode(content, true), title: page.title};
    }
    function mountDetail(page) {
        shell.replaceChildren(page.content);
        window.initializeComicDetail();
    }
    function prepareShell() {
        viewport.classList.add('is-preparing');
        viewport.setAttribute('aria-hidden', 'true');
        shell.inert = true;
        shell.hidden = false;
        viewport.hidden = false;
        viewport.scrollTo({left: 0, behavior: 'auto'});
        shell.scrollTop = 0;
    }
    function warmDetail(url) {
        if (!mobile.matches || active || pending || document.hidden) return;
        if (currentUrl === url) {
            warmed?.controller.abort(); warmed = null;
            prepareShell();
            return;
        }
        if (warmed?.url === url) return;
        warmed?.controller.abort();
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 10000);
        const prepared = {url, controller, mounted: false};
        warmed = prepared;
        prepared.page = fetchDetail(url, controller.signal).then(parseDetail).then(page => {
            if (warmed === prepared && !active && !pending && mobile.matches && !document.hidden) {
                prepareShell();
                mountDetail(page);
                currentUrl = null;
                prepared.mounted = true;
                // Decode the already shared cover before the user opens the page.
                shell.querySelectorAll('.md-detail-hero-media img, .md-detail-cover img').forEach(image => {
                    image.decode?.().catch(() => {});
                });
            }
            return page;
        }).finally(() => clearTimeout(timeout));
        prepared.page.catch(() => {
            if (warmed === prepared) warmed = null;
        });
    }
    function scheduleWarm() {
        clearTimeout(warmTimer);
        // Wait until the cover settles; retain only its detail, never the whole library.
        warmTimer = setTimeout(() => {
            if (!mobile.matches || active || pending || document.hidden) return;
            const link = home.querySelector('.md-hero-native-panel:not([inert]) .md-home-detail-link')
                || home.querySelector('.md-hero-copy .md-home-detail-link');
            if (link) warmDetail(link.href);
        }, 300);
    }
    home.addEventListener('md-home-hero-change', scheduleWarm);
    window.addEventListener('pageshow', scheduleWarm);
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) {
            clearTimeout(warmTimer);
            warmed?.controller.abort(); warmed = null;
            if (!active) currentUrl = null;
        } else scheduleWarm();
    });
    scheduleWarm();
    // Other library entries still prepare on touch intent.
    document.addEventListener('pointerdown', event => {
        if (!mobile.matches || active || pending || event.button !== 0 || event.metaKey
                || event.ctrlKey || event.shiftKey || event.altKey) return;
        const link = event.target.closest?.('a[href]');
        if (!link || !home.contains(link) || link.hasAttribute('download')
                || (link.target && link.target !== '_self')) return;
        const url = new URL(link.href, location.href);
        if (url.origin !== location.origin || !url.pathname.startsWith('/comic/')) return;
        warmDetail(url.href);
    }, {passive: true});
    document.addEventListener('click', event => {
        const link = event.target.closest?.('a[href]');
        if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey
                || event.shiftKey || event.altKey || link.hasAttribute('download')
                || (link.target && link.target !== '_self')) return;
        const url = new URL(link.href, location.href);
        if (pending && !url.pathname.startsWith('/comic/')) {
            pending.abort(); pending = null; resetFeedback();
        }
        if (url.origin !== location.origin) return;
        if (active && shell.contains(link) && url.pathname === location.pathname && url.hash) {
            const target = shell.querySelector(`#${CSS.escape(decodeURIComponent(url.hash.slice(1)))}`);
            if (target) {
                event.preventDefault();
                target.scrollIntoView({behavior: reduced.matches ? 'auto' : 'smooth', block: 'start'});
            }
            return;
        }
        if (active && ((shell.contains(link) && link.matches('.md-detail-back')) || url.href === homeUrl)) {
            event.preventDefault();
            if (motion || homeRequested || !ready) return;
            requestHome();
            return;
        }
        if (!active && home.contains(link) && url.pathname.startsWith('/comic/')) {
            event.preventDefault();
            if (pending || motion) return;
            opener = link;
            link.classList.add('is-loading'); link.setAttribute('aria-busy', 'true');
            loadDetail(url.href, true);
        }
    }, true);
    window.addEventListener('popstate', () => {
        homeRequested = false;
        const page = history.state?.mdComicPage;
        if (!page || page.home !== homeUrl) { location.reload(); return; }
        if (page.type === 'home') showHome();
        else {
            returnByPush = page.returnByPush === true;
            if (currentUrl === location.href) showDetail();
            else loadDetail(location.href, false);
        }
    });
    window.addEventListener('pagehide', () => {
        ++sequence;
        pending?.abort(); pending = null;
        warmed?.controller.abort(); warmed = null;
        if (!active) currentUrl = null;
        clearTimeout(warmTimer);
        clearTimeout(settleTimer);
        motion?.cancel();
        finishMotion(); resetFeedback();
    });
    // A reader/form can leave this document; restore a usable page on BFCache return.
    window.addEventListener('pageshow', event => {
        if (!event.persisted) return;
        if (history.state?.mdComicPage?.type === 'detail') {
            shell.hidden = false;
            viewport.hidden = false;
            viewport.scrollTo({left: viewport.clientWidth, behavior: 'auto'});
            window.resetReaderEntryButtons(); window.resetDetailSubmitButtons();
        } else if (active) showHome();
    });
    if (restored) {
        shell.replaceChildren(restored.detail);
        shell.hidden = false;
        shell.scrollTop = restored.detailScroll || 0;
        viewport.hidden = false;
        viewport.scrollTo({left: viewport.clientWidth, behavior: 'auto'});
        currentUrl = restored.detailUrl; currentTitle = restored.detailTitle;
        active = true;
        savedOverflow = document.body.style.overflow;
        document.body.style.overflow = 'hidden';
        document.body.classList.add('md-in-app-detail-open');
        home.inert = true; home.setAttribute('aria-hidden', 'true');
        if (dock) dock.hidden = true;
        history.replaceState(state('detail'), '', currentUrl);
        return () => {
            ready = true;
            history.pushState(state('home'), '', homeUrl);
            return showHome();
        };
    }
}
initializeHomeDetailNavigation();

// A detail loaded after leaving the reader has no retained homepage yet.
// Mount it once, then use the same transition and history controller as a home entry.
(() => {
    const detail = document.querySelector('body.comic-detail-body > .app-main-content');
    if (!detail || !window.fetch) return;
    let loading = false, mounted = false;
    document.addEventListener('click', async event => {
        const link = event.target.closest?.('.md-detail-back');
        if (mounted || !link || event.defaultPrevented || event.button !== 0 || event.metaKey
                || event.ctrlKey || event.shiftKey || event.altKey) return;
        const target = new URL(link.href, location.href);
        if (target.origin !== location.origin) return;
        event.preventDefault();
        if (loading) return;
        loading = true;
        link.classList.add('is-loading'); link.setAttribute('aria-busy', 'true');
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 15000);
        try {
            const response = await fetch(target.href, {credentials: 'same-origin', signal: controller.signal});
            if (!response.ok || response.redirected) throw new Error('Use full navigation');
            const page = new DOMParser().parseFromString(await response.text(), 'text/html');
            const home = page.querySelector('body.md-home-page > .app-main-content');
            if (!home) throw new Error('Missing homepage');
            const detailUrl = location.href, detailTitle = document.title, detailScroll = scrollY;
            const mountedHome = document.importNode(home, true);
            document.body.appendChild(mountedHome);
            const dock = page.querySelector('.md-mobile-tabbar');
            if (dock && !document.querySelector('.md-mobile-tabbar')) {
                const node = document.importNode(dock, true);
                document.body.appendChild(node);
                window.initializeMobileDock?.(node);
            }
            document.body.classList.remove('detail-body', 'comic-detail-body');
            document.body.classList.add('md-home-page');
            document.body.classList.toggle('has-mobile-tabbar', page.body.classList.contains('has-mobile-tabbar'));
            document.querySelectorAll('.md-topnav .md-nav-link').forEach(item => {
                item.classList.toggle('is-active', new URL(item.href, location.href).pathname === target.pathname);
            });
            document.documentElement.classList.remove('md-detail-entering');
            const position = window.mdReaderHomePosition;
            const homePosition = position?.url === target.href ? position : null;
            const finish = initializeHomeDetailNavigation({home: mountedHome, detail, detailUrl, detailTitle, detailScroll,
                homeUrl: target.href, homeTitle: page.title, homeScroll: homePosition?.scroll});
            mounted = true;
            // Only the explicitly marked homepage initializers run. Shared scripts stay bound once.
            for (const original of page.querySelectorAll('script[data-home-init]')) {
                const script = document.createElement('script');
                if (original.src) {
                    const source = new URL(original.getAttribute('src'), target.href);
                    if (source.origin !== location.origin) throw new Error('Unexpected script origin');
                    await new Promise((resolve, reject) => {
                        script.onload = resolve; script.onerror = reject;
                        script.src = source.href; document.body.appendChild(script);
                    });
                } else {
                    script.textContent = original.textContent;
                    document.body.appendChild(script);
                }
            }
            if (homePosition) {
                mountedHome.querySelector('.md-hero-native')?.scrollTo({left: homePosition.hero || 0, behavior: 'auto'});
            }
            link.classList.remove('is-loading'); link.removeAttribute('aria-busy');
            await finish();
        } catch (error) {
            location.assign(target.href);
        } finally {
            clearTimeout(timeout);
        }
    }, true);
})();
