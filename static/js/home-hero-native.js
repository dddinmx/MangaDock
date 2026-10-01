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
    const dots = [...hero.querySelectorAll('.md-hero-indicator')];
    const cards = [...hero.querySelectorAll('.md-home-rail__card')];
    let active = 0;
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
            // Native lazy loading anticipates pages during continuous scrolling.
            image.loading = 'lazy';
            image.fetchPriority = 'low';
            image.src = slide.artMobile || slide.art || slide.cover;
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

    const measureCopy = (index) => {
        const description = panels[index].querySelector('.md-hero-desc');
        const toggle = panels[index].querySelector('[data-hero-description-toggle]');
        toggle.hidden = !description.classList.contains('is-expanded')
            && description.scrollHeight <= description.clientHeight + 1;
    };
    const load = (index) => {
        if (loads.has(index)) return loads.get(index);
        const image = images[index];
        image.loading = 'eager';
        const promise = new Promise((resolve) => {
            const finish = () => {
                if (image.decode) image.decode().catch(() => {}).then(resolve);
                else resolve();
            };
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
        // Prioritize the visible page, then upcoming pages. Never clear a loaded src:
        // native scrolling can return before another request finishes.
        [0, direction, -direction, direction * 2, -direction * 2, direction * 3]
            .map((offset) => index + offset)
            .filter((target) => target >= 0 && target < slides.length)
            .forEach(load);
        panels.forEach((panel, n) => panel.classList.toggle('is-near', Math.abs(n - index) <= 1));
    };
    const update = (index) => {
        active = index;
        dots.forEach((dot, n) => {
            dot.classList.toggle('is-active', n === index);
            dot.setAttribute('aria-current', String(n === index));
        });
        cards.forEach((card, n) => card.classList.toggle('is-current', n === index));
        panels.forEach((panel, n) => { panel.inert = n !== index; });
        measureCopy(index);
        warm(index);
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
        panels.forEach((_, index) => measureCopy(index));
    };
    window.addEventListener('resize', resize, {passive: true});
    if (document.fonts?.ready) document.fonts.ready.then(() => panels.forEach((_, index) => measureCopy(index)));
    window.requestAnimationFrame(() => {
        viewportWidth = viewport.clientWidth;
        panels.forEach((_, index) => measureCopy(index));
        update(0);
    });
})();
