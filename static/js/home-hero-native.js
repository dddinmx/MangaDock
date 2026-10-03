(() => {
    const hero = document.querySelector('.md-home-page .md-hero--figma');
    const data = document.getElementById('md-home-hero-slides');
    if (!hero || !data || !window.matchMedia('(max-width: 767px)').matches) return;
    let slides;
    try { slides = JSON.parse(data.textContent); } catch (_) { return; }
    if (!Array.isArray(slides) || slides.length < 2) return;
    const inner = hero.querySelector('.md-hero-inner');
    const originalCopy = hero.querySelector('.md-hero-copy');
    const originalArt = hero.querySelector('.md-hero-art-current');
    const oldArt = hero.querySelector('.md-hero-art');
    const oldZone = hero.querySelector('.md-hero-swipe-zone');
    if (!inner || !originalCopy || !originalArt || !oldArt) return;

    const viewport = document.createElement('div');
    viewport.className = 'md-hero-native';
    viewport.tabIndex = 0;
    viewport.setAttribute('role', 'region');
    viewport.setAttribute('aria-label', '左右滑动切换最近观看的漫画');
    const template = originalCopy.cloneNode(true);
    const panels = [];
    const images = [];
    const loads = new Map();
    const decodes = new Map();
    const dots = [...hero.querySelectorAll('.md-hero-indicator')];
    const cards = [...hero.querySelectorAll('.md-home-rail__card')];
    let active = 0;
    let updatedIndex = -1;
    let preparedIndex = -1;
    let viewportWidth = 0;
    let settleTimer = 0;
    let busy = false;
    let pendingIndex = null;
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');

    const fill = (copy, slide) => {
        const find = (selector) => copy.querySelector(selector);
        find('[data-hero-title-text]').textContent = slide.name;
        find('[data-hero-title]').title = slide.name;
        find('[data-hero-format]').textContent = slide.format === 1 ? 'PDF' : slide.format === 2 ? 'CBZ' : '本地';
        find('[data-hero-adult]').hidden = !slide.adult;
        find('[data-hero-group]').textContent = slide.group;
        find('[data-hero-chapters]').textContent = `${slide.chapters} 话`;
        find('[data-hero-progress]').hidden = find('[data-hero-progress-sep]').hidden = !slide.hasProgress;
        find('[data-hero-progress]').textContent = slide.hasProgress
            ? `读到第 ${slide.lastChapter + 1} 话${slide.lastPage ? ` · 第 ${slide.lastPage + 1} 页` : ''}` : '';
        find('.md-hero-desc').textContent = slide.description || (slide.hasProgress
            ? `上次读到第 ${slide.lastChapter + 1} 话。点击继续，马上回到你离开的位置。`
            : '本地书库精选。一键开始阅读，进度会自动保存在当前账号下。');
        find('.md-home-detail-link').href = slide.detail;
        find('[data-hero-reader-link]').href = slide.reader;
        find('[data-hero-reader-label]').textContent = slide.hasProgress ? '继续阅读' : '开始阅读';
    };

    slides.forEach((slide, index) => {
        const panel = document.createElement('div');
        panel.className = 'md-hero-native-panel';
        panel.style.setProperty('--md-cover-blend', `url(${JSON.stringify(slide.cover)})`);
        const imageBox = document.createElement('div');
        imageBox.className = 'md-hero-native-art';
        const image = index === 0 ? originalArt : new Image();
        if (index === 0) {
            const url = image.currentSrc || image.src;
            image.removeAttribute('srcset');
            image.removeAttribute('sizes');
            image.src = url;
        }
        image.className = 'md-hero-native-image';
        image.alt = slide.name;
        image.decoding = 'async';
        image.draggable = false;
        if (index > 0) {
            // Assign src through warm(): early iOS 15 ignores native lazy loading
            // and would otherwise fetch every large cover on initial render.
            image.fetchPriority = 'low';
        }
        imageBox.appendChild(image);
        panel.appendChild(imageBox);
        const copy = index === 0 ? originalCopy : template.cloneNode(true);
        if (index > 0) {
            copy.querySelectorAll('[id]').forEach((node) => node.removeAttribute('id'));
            copy.querySelector('[data-hero-title]').removeAttribute('aria-live');
            copy.querySelector('[data-hero-description-toggle]').removeAttribute('aria-controls');
            fill(copy, slide);
            const description = copy.querySelector('.md-hero-desc');
            const toggle = copy.querySelector('[data-hero-description-toggle]');
            toggle.addEventListener('click', () => {
                const expanded = description.classList.toggle('is-expanded');
                toggle.setAttribute('aria-expanded', String(expanded));
                toggle.textContent = expanded ? '收起简介' : '展开简介';
            });
        }
        copy.classList.add('md-hero-native-copy');
        panel.appendChild(copy);
        viewport.appendChild(panel);
        panels.push(panel);
        images.push(image);
    });
    oldArt.hidden = true;
    if (oldZone) oldZone.hidden = true;
    inner.appendChild(viewport);
    hero.dataset.nativeCarousel = 'true';
    hero.classList.add('md-hero--native');

    const measureCopies = (indices) => {
        const measurements = indices.map(index => {
            const description = panels[index].querySelector('.md-hero-desc');
            return {
                toggle: panels[index].querySelector('[data-hero-description-toggle]'),
                hidden: !description.classList.contains('is-expanded')
                    && description.scrollHeight <= description.clientHeight + 1,
            };
        });
        measurements.forEach(({toggle, hidden}) => {
            if (toggle.hidden !== hidden) toggle.hidden = hidden;
        });
    };
    const load = (index) => {
        if (loads.has(index)) return loads.get(index);
        const image = images[index];
        image.loading = 'eager';
        const promise = new Promise((resolve) => {
            const finish = () => resolve();
            image.addEventListener('load', finish, {once: true});
            let fallbackAttempted = false;
            image.onerror = () => {
                if (!fallbackAttempted) {
                    fallbackAttempted = true;
                    image.src = slides[index].cover;
                } else resolve();
            };
            if (!image.getAttribute('src')) {
                image.fetchPriority = index === active ? 'high' : 'low';
                image.src = slides[index].artMobile || slides[index].art || slides[index].cover;
            } else if (image.complete && image.naturalWidth > 0) finish();
        });
        loads.set(index, promise);
        return promise;
    };
    const warm = (index) => {
        if (preparedIndex === index) return;
        const direction = preparedIndex < 0 || index >= preparedIndex ? 1 : -1;
        preparedIndex = index;
        // Refresh at page boundaries, not on every frame or only after scroll settles.
        prepareSurfaces(index);
        // Prioritize the visible page, then upcoming pages. Never clear a loaded src:
        // native scrolling can return before another request finishes.
        [0, direction, -direction, direction * 2, -direction * 2, direction * 3]
            .map((offset) => index + offset)
            .filter((target) => target >= 0 && target < slides.length)
            .forEach(load);
        const near = new Set([index - 1, index, index + 1]
            .filter(target => target >= 0 && target < slides.length));
        for (const target of decodes.keys()) if (!near.has(target)) decodes.delete(target);
        near.forEach(target => {
            if (decodes.has(target)) return;
            // A cached download is not a decoded surface: prepare it again on return.
            const promise = load(target).then(() => {
                if (decodes.get(target) !== promise) return;
                const image = images[target];
                if (image.naturalWidth > 0 && image.decode) return image.decode().catch(() => {
                    // A temporary decoder failure must not stay cached as ready.
                    if (decodes.get(target) === promise) decodes.delete(target);
                });
            });
            decodes.set(target, promise);
        });
    };
    const prepareSurfaces = (index, retainActive = false) => {
        panels.forEach((panel, n) => panel.classList.toggle('is-near',
            Math.abs(n - index) <= 1 || (retainActive && Math.abs(n - active) <= 1)));
    };
    const update = (index) => {
        if (updatedIndex === index) return;
        updatedIndex = index;
        active = index;
        // Finalize selection after settling; visible surfaces were already prepared.
        prepareSurfaces(index);
        dots.forEach((dot, n) => {
            dot.classList.toggle('is-active', n === index);
            dot.setAttribute('aria-current', String(n === index));
        });
        cards.forEach((card, n) => card.classList.toggle('is-current', n === index));
        panels.forEach((panel, n) => {
            // Keep selectors correct even before Safari supported the inert property.
            panel.toggleAttribute('inert', n !== index);
            panel.setAttribute('aria-hidden', String(n !== index));
        });
        measureCopies([index]);
        warm(index);
        hero.dispatchEvent(new Event('md-home-hero-change', {bubbles: true}));
    };
    const settle = () => {
        busy = false;
        window.clearTimeout(settleTimer);
        const width = viewport.clientWidth;
        if (width) update(Math.max(0, Math.min(slides.length - 1, Math.round(viewport.scrollLeft / width))));
        if (pendingIndex !== null) {
            const index = pendingIndex;
            pendingIndex = null;
            go(index);
        }
    };
    const go = (index) => {
        index = Math.max(0, Math.min(slides.length - 1, index));
        warm(index);
        prepareSurfaces(index, true);
        viewport.scrollTo({left: index * viewport.clientWidth, behavior: reduced.matches ? 'auto' : 'smooth'});
    };
    // Scrolling stays native. Advance preloading only when crossing into a new page.
    viewport.addEventListener('scroll', () => {
        busy = true;
        if (viewportWidth) {
            const approaching = Math.max(0, Math.min(slides.length - 1, Math.round(viewport.scrollLeft / viewportWidth)));
            warm(approaching);
        }
        window.clearTimeout(settleTimer);
        settleTimer = window.setTimeout(settle, 140);
    }, {passive: true});
    viewport.addEventListener('scrollend', settle);
    viewport.addEventListener('keydown', (event) => {
        if (event.target !== viewport || !['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
        event.preventDefault();
        go(active + (event.key === 'ArrowRight' ? 1 : -1));
    });
    dots.forEach((dot, index) => dot.addEventListener('click', () => go(index)));
    const resize = () => {
        viewportWidth = viewport.clientWidth;
        if (busy) { pendingIndex = active; return; }
        viewport.scrollTo({left: active * viewport.clientWidth, behavior: 'auto'});
        measureCopies(panels.map((_, index) => index));
    };
    window.addEventListener('resize', resize, {passive: true});
    if (document.fonts?.ready) document.fonts.ready.then(() => measureCopies(panels.map((_, index) => index)));
    window.requestAnimationFrame(() => {
        viewportWidth = viewport.clientWidth;
        measureCopies(panels.map((_, index) => index));
        update(0);
    });
})();
