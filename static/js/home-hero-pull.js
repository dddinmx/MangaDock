(() => {
    const hero = document.querySelector('.md-home-page .md-hero--figma');
    if (!hero || hero.dataset.pullZoomBound === 'true') return;
    hero.dataset.pullZoomBound = 'true';
    const mobile = window.matchMedia('(max-width: 767px)');
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
    let frame = 0;
    let prepareFrame = 0;
    let preparedImage = null;
    let image = null;
    let height = 0;
    let original = null;
    let lastScale = 1;
    let gesture = null;
    let motion = null;

    const enabled = () => document.documentElement.dataset.coverMotion === 'on' && mobile.matches && !reduced.matches && !document.hidden
        && !document.body.classList.contains('md-in-app-detail-open');
    const restore = () => {
        if (frame) window.cancelAnimationFrame(frame);
        frame = 0;
        if (motion) motion.cancel();
        motion = null;
        if (image) image.style.transform = original;
        image = original = null;
        lastScale = 1;
    };
    const reset = () => {
        restore();
        gesture = null;
        if (prepareFrame) window.cancelAnimationFrame(prepareFrame);
        prepareFrame = 0;
    };
    const prepare = () => {
        reset();
        preparedImage = null;
        height = 0;
        const active = document.documentElement.dataset.coverMotion === 'on' && mobile.matches && !reduced.matches;
        hero.dataset.pullZoomEnabled = String(active);
        if (!active) return;
        prepareFrame = window.requestAnimationFrame(() => {
            prepareFrame = 0;
            preparedImage = hero.querySelector('.md-hero-native-panel:not([inert]) .md-hero-native-image')
                || hero.querySelector('.md-hero-art-current');
            height = preparedImage?.clientHeight || 0;
        });
    };
    const update = () => {
        frame = 0;
        if (!gesture?.pulling || !enabled()) {
            reset();
            return;
        }
        const pull = Math.max(0, -window.scrollY);
        if (!pull) {
            restore();
            return;
        }
        if (!image) {
            if (!preparedImage || !height) return;
            image = preparedImage;
            original = image.style.transform;
        }
        const scale = 1 + Math.min(pull / height, .35);
        if (scale !== lastScale) {
            image.style.transform = `translateZ(0) scale(${scale})`;
            lastScale = scale;
        }
    };
    const schedule = () => {
        // Negative scrollY also occurs during inertia: only a deliberate pull may zoom.
        if (!gesture?.pulling || !enabled()) return;
        if (!frame) frame = window.requestAnimationFrame(update);
    };
    hero.addEventListener('touchstart', event => {
        restore();
        gesture = null;
        if (!enabled() || event.touches.length !== 1) return;
        const touch = event.touches[0];
        gesture = {x: touch.clientX, y: touch.clientY, anchor: touch.clientY,
            atTop: window.scrollY <= 0, direction: null, pulling: false};
    }, {passive: true});
    window.addEventListener('touchmove', event => {
        if (!gesture) return;
        if (event.touches.length !== 1 || !enabled()) {
            reset();
            return;
        }
        const touch = event.touches[0];
        const dx = touch.clientX - gesture.x;
        const dy = touch.clientY - gesture.y;
        if (!gesture.direction && Math.max(Math.abs(dx), Math.abs(dy)) >= 12) {
            gesture.direction = dy > 0 && dy > Math.abs(dx) ? 'down' : 'ignore';
        }
        if (gesture.direction !== 'down') return;
        if (window.scrollY > 0) {
            gesture.anchor = touch.clientY;
            gesture.atTop = false;
            gesture.pulling = false;
            restore();
            return;
        }
        if (!gesture.atTop) {
            gesture.anchor = touch.clientY;
            gesture.atTop = true;
        }
        if (touch.clientY - gesture.anchor >= 12) gesture.pulling = true;
        schedule();
    }, {passive: true});
    window.addEventListener('touchend', () => {
        gesture = null;
        const releasedImage = image;
        const from = image?.style.transform;
        const to = original || 'translateZ(0) scale(1)';
        restore();
        // Let the browser settle this one transform; do not track every native bounce frame.
        if (!releasedImage || !enabled() || !releasedImage.animate) return;
        const animation = releasedImage.animate([{transform: from}, {transform: to}], {
            duration: 240, easing: 'cubic-bezier(.22, 1, .36, 1)',
        });
        motion = animation;
        animation.finished.then(() => {
            if (motion === animation) {
                animation.cancel();
                motion = null;
            }
        }, () => {});
    }, {passive: true});
    window.addEventListener('touchcancel', reset, {passive: true});
    window.addEventListener('scroll', schedule, {passive: true});
    window.addEventListener('resize', prepare, {passive: true});
    window.addEventListener('md-cover-motion-change', prepare);
    window.addEventListener('pagehide', reset);
    window.addEventListener('pageshow', prepare);
    document.addEventListener('visibilitychange', () => document.hidden ? reset() : prepare());
    hero.addEventListener('md-home-hero-change', prepare);
    mobile.addEventListener('change', prepare);
    reduced.addEventListener('change', prepare);
    prepare();
})();
