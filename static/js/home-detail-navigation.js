function initializeHomeDetailNavigation(restored = null) {
    const home = restored?.home || document.querySelector('body.md-home-page > .app-main-content');
    if (!home || !window.fetch || !Element.prototype.animate) return;
    const homeUrl = restored?.homeUrl || location.href;
    const homeTitle = restored?.homeTitle || document.title;
    const reduced = matchMedia('(prefers-reduced-motion: reduce)');
    const dock = document.querySelector('.md-mobile-tabbar');
    const shell = document.createElement('section');
    shell.className = 'md-in-app-detail';
    shell.hidden = true;
    shell.tabIndex = -1;
    shell.setAttribute('aria-label', '漫画详情');
    document.body.appendChild(shell);
    let currentUrl = null, currentTitle = '', pending = null, active = false;
    let motion = null, sequence = 0, opener = null;
    let savedOverflow = '', savedScroll = 0;
    let returnByPush = !!restored;
    const state = type => ({...history.state, mdComicPage: {type, home: homeUrl,
        returnByPush: type === 'detail' && returnByPush}});
    if (!restored) history.replaceState(state('home'), '', homeUrl);
    let ready = !restored;

    function resetFeedback() {
        opener?.classList.remove('is-loading');
        opener?.removeAttribute('aria-busy');
    }
    function animate(enter) {
        const from = motion ? getComputedStyle(shell).transform
            : enter ? 'translateX(100%)' : 'translateX(0)';
        motion?.cancel();
        const animation = shell.animate([
            {transform: from}, {transform: enter ? 'translateX(0)' : 'translateX(100%)'}
        ], {
            duration: reduced.matches ? 0 : 380,
            easing: 'cubic-bezier(0.25, 0.1, 0.25, 1)', fill: 'both'
        });
        motion = animation;
        return animation.finished.catch(() => {});
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
        requestAnimationFrame(window.refreshComicSynopsisLayout);
        document.title = currentTitle;
        await animate(true);
        if (ticket !== sequence) return;
        home.style.visibility = 'hidden';
        motion?.cancel(); motion = null;
        shell.focus({preventScroll: true});
        resetFeedback();
    }
    async function showHome() {
        const ticket = ++sequence;
        pending?.abort(); pending = null;
        resetFeedback();
        document.title = homeTitle;
        if (!active) return;
        home.style.visibility = '';
        await animate(false);
        if (ticket !== sequence) return;
        shell.hidden = true;
        motion?.cancel(); motion = null;
        active = false;
        document.body.style.overflow = savedOverflow;
        document.body.classList.remove('md-in-app-detail-open');
        home.removeAttribute('aria-hidden'); home.inert = false;
        if (dock) dock.hidden = false;
        scrollTo(0, savedScroll);
        opener?.focus({preventScroll: true});
    }
    async function loadDetail(url, push) {
        pending?.abort();
        const controller = new AbortController();
        pending = controller;
        const timeout = setTimeout(() => controller.abort(), 15000);
        try {
            const response = await fetch(url, {credentials: 'same-origin', signal: controller.signal});
            if (!response.ok || response.redirected) throw new Error('Use full navigation');
            const page = new DOMParser().parseFromString(await response.text(), 'text/html');
            const content = page.querySelector('.app-main-content');
            if (!content?.querySelector('.comic-detail')) throw new Error('Missing detail');
            if (pending !== controller) return;
            // Import only the content. Shared scripts and event delegation run once.
            content.querySelectorAll('script').forEach(script => script.remove());
            shell.replaceChildren(document.importNode(content, true));
            shell.scrollTop = 0;
            currentUrl = url; currentTitle = page.title;
            window.initializeComicDetail();
            const cover = shell.querySelector('.md-detail-cover img');
            if (cover?.decode) await Promise.race([cover.decode().catch(() => {}), new Promise(resolve => setTimeout(resolve, 400))]);
            if (pending !== controller) return;
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
            if (motion || !ready) return;
            if (returnByPush) {
                history.pushState(state('home'), '', homeUrl);
                showHome();
            } else {
                history.back();
            }
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
        const page = history.state?.mdComicPage;
        if (!page || page.home !== homeUrl) { location.reload(); return; }
        if (page.type === 'home') showHome();
        else {
            returnByPush = page.returnByPush === true;
            if (currentUrl === location.href) showDetail();
            else loadDetail(location.href, false);
        }
    });
    window.addEventListener('pagehide', () => { pending?.abort(); pending = null; resetFeedback(); });
    // A reader/form can leave this document; restore a usable page on BFCache return.
    window.addEventListener('pageshow', event => {
        if (!event.persisted) return;
        if (history.state?.mdComicPage?.type === 'detail') {
            window.resetReaderEntryButtons(); window.resetDetailSubmitButtons();
        } else if (active) showHome();
    });
    if (restored) {
        shell.replaceChildren(restored.detail);
        shell.hidden = false;
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
// Mount it once, then use the same slide and history controller as a home entry.
(() => {
    const detail = document.querySelector('body.comic-detail-body > .app-main-content');
    if (!detail || !window.fetch || !Element.prototype.animate) return;
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
            const detailUrl = location.href, detailTitle = document.title;
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
            const finish = initializeHomeDetailNavigation({home: mountedHome, detail, detailUrl, detailTitle,
                homeUrl: target.href, homeTitle: page.title});
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
            link.classList.remove('is-loading'); link.removeAttribute('aria-busy');
            await finish();
        } catch (error) {
            location.assign(target.href);
        } finally {
            clearTimeout(timeout);
        }
    }, true);
})();
