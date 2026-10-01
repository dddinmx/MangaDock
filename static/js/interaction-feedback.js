(() => {
    // Delegation also covers controls inserted after page load.
    const selector = 'button, a[href], input[type="button"], input[type="submit"], input[type="reset"], [role="button"], summary, label[for]';
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
    let pressed = null;
    let origin = null;
    const animations = new WeakMap();
    const control = (event) => {
        const node = event.target?.closest?.(selector);
        return node && !node.disabled && !node.closest('[inert]') && node.getAttribute('aria-disabled') !== 'true' ? node : null;
    };
    const release = () => {
        pressed?.classList.remove('md-control-pressed');
        pressed = origin = null;
    };
    document.addEventListener('pointerdown', (event) => {
        release();
        if (event.button !== 0) return;
        pressed = control(event);
        if (!pressed) return;
        animations.get(pressed)?.cancel();
        origin = {x: event.clientX, y: event.clientY};
        pressed.classList.add('md-control-pressed');
    }, {capture: true, passive: true});
    document.addEventListener('pointermove', (event) => {
        if (origin && Math.hypot(event.clientX - origin.x, event.clientY - origin.y) > 10) release();
    }, {passive: true});
    document.addEventListener('pointerup', release, {capture: true, passive: true});
    document.addEventListener('pointercancel', release, {capture: true, passive: true});
    document.addEventListener('keydown', (event) => {
        if (event.repeat || !['Enter', ' '].includes(event.key)) return;
        pressed = control(event);
        pressed?.classList.add('md-control-pressed');
    }, true);
    document.addEventListener('keyup', release, true);
    document.addEventListener('click', (event) => {
        const node = control(event);
        if (!node || reduced.matches || !node.animate) return;
        animations.get(node)?.cancel();
        // Opacity is composited; it does not replace positioning transforms.
        const animation = node.animate([{opacity: 0.62}, {opacity: 1}], {duration: 220, easing: 'ease-out'});
        animations.set(node, animation);
        animation.onfinish = () => animations.delete(node);
    }, true);
    window.addEventListener('blur', release);
    window.addEventListener('pageshow', release);
    document.addEventListener('visibilitychange', () => { if (document.hidden) release(); });
})();
