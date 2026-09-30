/* iOS 27 may place a standalone WebView under the system status-bar blur. */
(() => {
    const viewport = document.querySelector('meta[name="viewport"]');
    if (!viewport || !/viewport-fit\s*=\s*cover/i.test(viewport.content)) return;
    const isAppleMobile = /iPhone|iPad|iPod/i.test(navigator.userAgent || '')
        || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
    const isStandalone = () => window.matchMedia('(display-mode: standalone)').matches
        || navigator.standalone === true;
    if (!isAppleMobile || !isStandalone()) return;

    const probe = document.createElement('div');
    probe.setAttribute('aria-hidden', 'true');
    probe.style.cssText = 'position:fixed;visibility:hidden;pointer-events:none;'
        + 'padding-top:env(safe-area-inset-top,0px);'
        + 'padding-bottom:env(safe-area-inset-bottom,0px)';
    document.body.appendChild(probe);

    const check = () => {
        if (document.hidden) return;
        const style = window.getComputedStyle(probe);
        const safeTop = parseFloat(style.paddingTop) || 0;
        const screenHeight = Math.max(screen.width, screen.height);
        // A keyboard shrinks innerHeight; landscape should keep edge-to-edge reading.
        if (safeTop <= 0 || innerHeight <= innerWidth || innerHeight < screenHeight - 1) return;

        const safeBottom = parseFloat(style.paddingBottom) || 0;
        document.documentElement.style.setProperty('--md-pwa-safe-bottom', `${safeBottom}px`);
        viewport.content = viewport.content.split(',').map((part) => part.trim())
            .filter((part) => part && !/^viewport-fit\s*=/i.test(part)).join(', ');
        window.removeEventListener('resize', check);
        document.removeEventListener('visibilitychange', check);
        probe.remove();
    };

    window.addEventListener('resize', check, {passive: true});
    document.addEventListener('visibilitychange', check);
    check();
})();
