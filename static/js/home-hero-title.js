(() => {
    const hero = document.querySelector('.md-home-page .md-hero--figma');
    if (!hero || hero.dataset.titleFitBound === 'true') return;
    hero.dataset.titleFitBound = 'true';
    let frame = 0;

    const fitTitles = () => {
        frame = 0;
        const mobile = window.matchMedia('(max-width: 767px)').matches;
        const titles = [...hero.querySelectorAll('[data-hero-title]')];
        titles.forEach(title => { title.style.fontSize = ''; });
        if (!mobile) return;
        // Read every title before applying sizes, so one fit does not reflow the next.
        const sizes = titles.map(title => {
            const text = title.querySelector('[data-hero-title-text]');
            const width = title.clientWidth;
            if (!text || !width) return '';
            const size = parseFloat(window.getComputedStyle(title).fontSize);
            const textWidth = text.scrollWidth;
            return textWidth > width ? `${size * (width - 1) / textWidth}px` : '';
        });
        titles.forEach((title, index) => {
            if (sizes[index]) title.style.fontSize = sizes[index];
        });
    };
    const scheduleFit = () => {
        if (!frame) frame = window.requestAnimationFrame(fitTitles);
    };
    window.addEventListener('resize', scheduleFit, {passive: true});
    let observedWidth = hero.clientWidth;
    const observer = new ResizeObserver(([entry]) => {
        if (entry.contentRect.width === observedWidth) return;
        observedWidth = entry.contentRect.width;
        scheduleFit();
    });
    observer.observe(hero);
    // Native carousel titles are fitted together before a swipe, not during scrolling.
    scheduleFit();
    if (document.fonts?.ready) document.fonts.ready.then(scheduleFit);
})();
