(() => {
    const data = document.getElementById('md-home-hero-slides');
    const hero = document.querySelector('.md-home-page .md-hero--figma');
    if (!data || !hero || hero.dataset.nativeCarousel === 'true') return;

    let slides;
    try {
        slides = JSON.parse(data.textContent);
    } catch (error) {
        return;
    }
    if (!Array.isArray(slides) || slides.length < 2) return;

    const find = (selector) => hero.querySelector(selector);
    const title = find('[data-hero-title]');
    const description = find('#hero-comic-description');
    let art = find('.md-hero-art-current');
    let nextArt = find('.md-hero-art-next');
    const copy = find('.md-hero-copy');
    let background = find('.md-hero-media-img');
    const mobileSource = find('.md-hero-media source');
    const zone = find('.md-hero-swipe-zone');
    const indicators = find('.md-hero-indicators');
    const dots = [...hero.querySelectorAll('.md-hero-indicator')];
    const cards = [...hero.querySelectorAll('.md-home-rail__card')];
    if (!title || !art || !nextArt || !copy || !background || !zone) return;

    let activeIndex = 0;
    let requestedIndex = 0;
    let requestedDirection = 1;
    let requestId = 0;
    let sliding = false;
    let stagedIndex = -1;
    const artLoads = new Map();
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const mobileArt = window.matchMedia('(max-width: 767px)').matches;
    const slideDuration = mobileArt ? 260 : 360;
    const slideEasing = 'cubic-bezier(0.22, 1, 0.36, 1)';
    let nextBackground = null;
    if (!mobileArt) {
        // Blend two already decoded covers; never animate the expensive blur filter.
        mobileSource?.remove();
        nextBackground = background.cloneNode(false);
        nextBackground.removeAttribute('srcset');
        // Keep the standby surface rasterized before the transition starts.
        nextBackground.style.opacity = '0.001';
        background.closest('.md-hero-media').appendChild(nextBackground);
    }
    const nextCopy = copy.cloneNode(true);
    nextCopy.classList.add('md-hero-copy-next');
    nextCopy.removeAttribute('id');
    nextCopy.querySelector('#hero-comic-description')?.removeAttribute('id');
    nextCopy.setAttribute('aria-hidden', 'true');
    nextCopy.setAttribute('inert', '');
    nextCopy.hidden = true;
    copy.parentElement.appendChild(nextCopy);

    const fillCopy = (node, slide) => {
        const item = (selector) => node.querySelector(selector);
        item('[data-hero-title-text]').textContent = slide.name;
        item('[data-hero-title]').title = slide.name;
        item('[data-hero-format]').textContent = slide.format === 1 ? 'PDF' : slide.format === 2 ? 'CBZ' : '本地';
        item('[data-hero-adult]').hidden = !slide.adult;
        item('[data-hero-group]').textContent = slide.group;
        item('[data-hero-chapters]').textContent = `${slide.chapters} 话`;
        item('[data-hero-progress]').hidden = item('[data-hero-progress-sep]').hidden = !slide.hasProgress;
        item('[data-hero-progress]').textContent = slide.hasProgress
            ? `读到第 ${slide.lastChapter + 1} 话${slide.lastPage ? ` · 第 ${slide.lastPage + 1} 页` : ''}`
            : '';
        const itemDescription = item('.md-hero-desc');
        itemDescription.textContent = slide.description || (slide.hasProgress
            ? `上次读到第 ${slide.lastChapter + 1} 话${slide.lastPage ? ` · 第 ${slide.lastPage + 1} 页` : ''}。点击继续，马上回到你离开的位置。`
            : '本地书库精选。一键开始阅读，进度会自动保存在当前账号下。');
        itemDescription.classList.remove('is-expanded');
        const toggle = item('[data-hero-description-toggle]');
        if (toggle) {
            toggle.textContent = '展开简介';
            toggle.setAttribute('aria-expanded', 'false');
        }
        item('.md-home-detail-link').href = slide.detail;
        item('[data-hero-reader-link]').href = slide.reader;
        item('[data-hero-reader-label]').textContent = slide.hasProgress ? '继续阅读' : '开始阅读';
    };

    const warmImage = (loads, index, url, fallback) => {
        if (loads.has(index)) return loads.get(index).promise;
        const entry = {url: '', image: null, promise: null};
        loads.set(index, entry);
        entry.promise = new Promise((resolve) => {
            const image = new Image();
            image.decoding = 'async';
            image.fetchPriority = 'low';
            entry.image = image;
            const done = (loadedUrl) => {
                entry.url = loadedUrl;
                resolve(loadedUrl);
            };
            image.onload = () => {
                if (typeof image.decode === 'function') image.decode().then(() => done(image.src), () => done(image.src));
                else done(image.src);
            };
            let fallbackAttempted = false;
            image.onerror = () => {
                if (!fallbackAttempted) {
                    fallbackAttempted = true;
                    image.src = fallback;
                } else done(fallback);
            };
            image.src = url;
        });
        return entry.promise;
    };

    const warmArt = (index) => warmImage(artLoads, index,
        (mobileArt && slides[index].artMobile) || slides[index].art || slides[index].cover,
        slides[index].cover);
    const readyUrl = (index) => artLoads.get(index)?.url || '';

    const setArtSource = (node, imageUrl) => {
        // 使用刚刚解码完成的同一个 URL，避免 srcset 在高 DPR 手机上再切换资源。
        node.removeAttribute('srcset');
        node.removeAttribute('sizes');
        node.src = imageUrl;
    };

    const syncCopyToggle = (node) => {
        const itemDescription = node.querySelector('.md-hero-desc');
        const toggle = node.querySelector('[data-hero-description-toggle]');
        if (itemDescription && toggle) {
            toggle.hidden = itemDescription.scrollHeight <= itemDescription.clientHeight + 1;
        }
    };

    const warmNeighbors = (index) => {
        const next = (index + 1) % slides.length;
        const previous = (index - 1 + slides.length) % slides.length;
        warmArt(next).then((url) => {
            const stage = () => { if (!sliding && activeIndex === index) stageSlide(next, url, 1); };
            if (window.requestIdleCallback) window.requestIdleCallback(stage, {timeout: 400});
            else window.setTimeout(stage, 60);
        });
        if (previous !== next) warmArt(previous);
        const ahead = (index + 2) % slides.length;
        const behind = (index - 2 + slides.length) % slides.length;
        // Five retained decoded images bound memory even with a large recent list.
        const retained = new Set([index, next, previous, ahead, behind]);
        for (const key of artLoads.keys()) if (!retained.has(key)) artLoads.delete(key);
        warmArt(next).then(() => { if (activeIndex === index) warmArt(ahead); });
        warmArt(previous).then(() => { if (activeIndex === index) warmArt(behind); });
    };

    const showSlide = (index, artUrl, updateImage = true) => {
        const slide = slides[index];
        activeIndex = index;
        fillCopy(copy, slide);
        if (updateImage) syncCopyToggle(copy);
        description.dispatchEvent(new Event('md-home-hero-change', {bubbles: true}));

        if (updateImage) setArtSource(art, artUrl);
        art.alt = slide.name;

        dots.forEach((dot, dotIndex) => {
            const current = dotIndex === index;
            dot.classList.toggle('is-active', current);
            dot.setAttribute('aria-current', String(current));
        });
        const activeDot = dots[index];
        if (activeDot && indicators && indicators.scrollWidth > indicators.clientWidth) {
            const left = indicators.scrollLeft + activeDot.getBoundingClientRect().left
                - indicators.getBoundingClientRect().left
                - (indicators.clientWidth - activeDot.clientWidth) / 2;
            indicators.scrollTo({left: Math.max(0, left), behavior: 'auto'});
        }
        cards.forEach((card, cardIndex) => card.classList.toggle('is-current', cardIndex === index));
        if (!mobileArt && updateImage) background.src = artUrl;
        warmNeighbors(index);
    };

    let motionToken = 0;
    const movingParts = () => [art, nextArt, copy, nextCopy];

    const setProgress = (direction, progress) => {
        const current = -direction * progress * 100;
        const incoming = direction * (1 - progress) * 100;
        art.style.transform = `translate3d(${current}%, 0, 0)`;
        nextArt.style.transform = `translate3d(${incoming}%, 0, 0)`;
        copy.style.transform = `translate3d(${current}vw, 0, 0)`;
        nextCopy.style.transform = `translate3d(${incoming}vw, 0, 0)`;
    };

    const stageSlide = (index, imageUrl, direction) => {
        if (nextBackground) {
            background.style.transition = nextBackground.style.transition = 'none';
            background.style.opacity = '';
            nextBackground.style.opacity = '0.001';
            if (nextBackground.getAttribute('src') !== imageUrl) nextBackground.src = imageUrl;
        }
        if (stagedIndex !== index) {
            fillCopy(nextCopy, slides[index]);
            nextCopy.hidden = false;
            // Reuse the decoded Image itself: setting another img.src can revalidate it.
            const prepared = artLoads.get(index)?.image;
            if (prepared && prepared !== art && prepared !== nextArt) {
                prepared.className = 'md-hero-art-next';
                prepared.setAttribute('aria-hidden', 'true');
                prepared.alt = slides[index].name;
                nextArt.replaceWith(prepared);
                nextArt = prepared;
            } else if (nextArt.src !== imageUrl) setArtSource(nextArt, imageUrl);
            const picture = art.parentElement;
            if (picture?.tagName === 'PICTURE' && picture.querySelector('source')) {
                const initialUrl = art.currentSrc || art.src;
                setArtSource(art, initialUrl);
                picture.querySelector('source')?.remove();
            }
            nextArt.hidden = false;
            movingParts().forEach((part) => { part.style.transition = 'none'; });
            setProgress(direction, 0);
            syncCopyToggle(nextCopy);
            stagedIndex = index;
        } else {
            movingParts().forEach((part) => { part.style.transition = 'none'; });
            setProgress(direction, 0);
        }
    };

    const prepareSlide = (index, imageUrl, direction) => {
        motionToken += 1;
        stageSlide(index, imageUrl, direction);
        sliding = true;
        hero.classList.add('is-sliding');
        return motionToken;
    };

    const finishSlide = (index, imageUrl, committed, token) => {
        if (token !== motionToken) return;
        movingParts().forEach((part) => { part.style.transition = 'none'; });
        if (committed) {
            if (nextBackground) [background, nextBackground] = [nextBackground, background];
            copy.querySelector('[data-hero-description-toggle]').hidden =
                nextCopy.querySelector('[data-hero-description-toggle]').hidden;
            const previousArt = art;
            art = nextArt;
            nextArt = previousArt;
            art.classList.remove('md-hero-art-next');
            art.classList.add('md-hero-art-current');
            art.removeAttribute('aria-hidden');
            nextArt.classList.remove('md-hero-art-current');
            nextArt.classList.add('md-hero-art-next');
            nextArt.setAttribute('aria-hidden', 'true');
        }
        art.style.transform = '';
        copy.style.transform = '';
        nextArt.hidden = true;
        // Keep the previous decoded image available for reversing direction.
        nextArt.style.transform = '';
        nextCopy.hidden = true;
        nextCopy.style.transform = '';
        hero.classList.remove('is-sliding', 'is-dragging');
        sliding = false;
        if (nextBackground) {
            background.style.transition = nextBackground.style.transition = 'none';
            background.style.opacity = '';
            nextBackground.style.opacity = '0.001';
        }
        stagedIndex = -1;
        if (committed) showSlide(index, imageUrl, false);
        else requestedIndex = activeIndex;
        const clearTransition = () => {
            if (token === motionToken && !sliding) {
                movingParts().forEach((part) => {
                    part.style.transition = '';
                    part.style.transitionDuration = '';
                });
            }
        };
        window.requestAnimationFrame(clearTransition);
        window.setTimeout(clearTransition, 24);
        if (committed && requestedIndex !== index) selectSlide(requestedIndex, requestedDirection);
    };

    const settleSlide = (index, imageUrl, committed, token, direction, duration) => {
        if (!mobileArt && typeof nextArt.animate === 'function') {
            const parts = movingParts();
            const starts = parts.map(part => part.style.transform || 'translate3d(0, 0, 0)');
            parts.forEach(part => { part.style.transition = 'none'; });
            const progress = committed ? 1 : 0;
            const current = -direction * progress * 100;
            const incoming = direction * (1 - progress) * 100;
            const ends = [`translate3d(${current}%, 0, 0)`, `translate3d(${incoming}%, 0, 0)`,
                `translate3d(${current}vw, 0, 0)`, `translate3d(${incoming}vw, 0, 0)`];
            const animations = parts.map((part, position) => part.animate([
                {transform: starts[position]}, {transform: ends[position]}
            ], {duration, easing: slideEasing, fill: 'both'}));
            if (nextBackground) {
                for (const [node, opacity] of [[background, committed ? '0' : '0.9'],
                        [nextBackground, committed ? '0.9' : '0.001']]) {
                    animations.push(node.animate([
                        {opacity: node === background ? '0.9' : '0.001'}, {opacity}
                    ], {duration, easing: 'ease', fill: 'both'}));
                }
            }
            let finished = false;
            const finish = () => {
                if (finished) return;
                finished = true;
                animations.forEach(animation => animation.cancel());
                finishSlide(index, imageUrl, committed, token);
            };
            Promise.all(animations.map(animation => animation.finished.catch(() => {}))).then(finish);
            return;
        }
        const incoming = nextArt;
        let finished = false;
        const finish = () => {
            if (finished || token !== motionToken) return;
            finished = true;
            incoming.removeEventListener('transitionend', onEnd);
            finishSlide(index, imageUrl, committed, token);
        };
        const onEnd = (event) => {
            if (event.target === incoming && event.propertyName === 'transform') finish();
        };
        incoming.addEventListener('transitionend', onEnd);
        // Explicit shorthand preserves the same duration used by the completion guard.
        movingParts().forEach((part) => {
            part.style.transition = `transform ${duration}ms ${slideEasing}`;
        });
        if (nextBackground) {
            background.style.transition = nextBackground.style.transition = `opacity ${duration}ms ease`;
            background.style.opacity = committed ? '0' : '';
            nextBackground.style.opacity = committed ? '0.9' : '0.001';
        }
        setProgress(direction, committed ? 1 : 0);
        window.setTimeout(finish, duration + 80);
    };

    const animateSlide = (index, imageUrl, direction) => {
        const token = prepareSlide(index, imageUrl, direction);
        if (!mobileArt && typeof nextArt.animate === 'function') {
            settleSlide(index, imageUrl, true, token, direction, slideDuration);
            return;
        }

        let started = false;
        const start = () => {
            if (started || token !== motionToken) return;
            started = true;
            settleSlide(index, imageUrl, true, token, direction, slideDuration);
        };
        window.requestAnimationFrame(() => window.requestAnimationFrame(start));
        window.setTimeout(start, 48);
    };

    const selectSlide = (index, direction) => {
        if (index < 0 || index >= slides.length) return;
        requestId += 1;
        const currentRequest = requestId;
        requestedIndex = index;
        requestedDirection = direction || (index > activeIndex ? 1 : -1);
        if (sliding) return;
        if (index === activeIndex) {
            hero.classList.remove('is-waiting-cover');
            zone.setAttribute('aria-busy', 'false');
            return;
        }
        hero.classList.toggle('is-waiting-cover', !readyUrl(index));
        zone.setAttribute('aria-busy', String(!readyUrl(index)));
        warmArt(index).then((imageUrl) => {
            if (currentRequest !== requestId || sliding || requestedIndex !== index) return;
            hero.classList.remove('is-waiting-cover');
            zone.setAttribute('aria-busy', 'false');
            // Desktop cover switching is an explicit user action: keep its slide
            // feedback independent of the optional page-navigation motion.
            if (reducedMotion && mobileArt) showSlide(index, imageUrl);
            else animateSlide(index, imageUrl, requestedDirection);
        });
    };

    const move = (direction) => {
        selectSlide((requestedIndex + direction + slides.length) % slides.length, direction);
    };

    const swipeFrom = (start, endX, endY) => {
        if (!start) return;
        const distanceX = endX - start.x;
        const distanceY = endY - start.y;
        if (Math.abs(distanceX) >= 45 && Math.abs(distanceX) > Math.abs(distanceY) * 1.25) {
            move(distanceX < 0 ? 1 : -1);
        }
    };

    let touchStart = null;
    let drag = null;
    let pendingDragX = 0;
    let dragFrame = 0;
    let dragScheduled = false;
    const paintDrag = () => {
        if (!dragScheduled) return;
        dragScheduled = false;
        if (dragFrame) window.cancelAnimationFrame(dragFrame);
        dragFrame = 0;
        if (!drag) return;
        const progress = Math.max(0, Math.min(1, -drag.direction * pendingDragX / drag.width));
        setProgress(drag.direction, progress);
    };
    const touchBegin = (event) => {
        if (sliding || (reducedMotion && mobileArt) || event.touches?.length > 1) return;
        if (event.target?.closest?.('a, button')) return;
        const touch = event.changedTouches[0];
        if (touch) touchStart = {id: touch.identifier, x: touch.clientX, y: touch.clientY, at: Date.now()};
    };
    const touchMove = (event) => {
        if (!touchStart) return;
        const touch = Array.from(event.changedTouches).find((item) => item.identifier === touchStart.id);
        if (!touch) return;
        const dx = touch.clientX - touchStart.x;
        const dy = touch.clientY - touchStart.y;
        if (!drag) {
            if (sliding || Math.abs(dx) < 8 || Math.abs(dx) <= Math.abs(dy) * 1.25) return;
            const direction = dx < 0 ? 1 : -1;
            const index = (activeIndex + direction + slides.length) % slides.length;
            const imageUrl = readyUrl(index);
            if (!imageUrl) {
                warmArt(index);
                return;
            }
            requestedIndex = index;
            requestedDirection = direction;
            requestId += 1;
            const token = prepareSlide(index, imageUrl, direction);
            hero.classList.add('is-dragging');
            drag = {index, direction, imageUrl, token, width: window.innerWidth || hero.clientWidth || 1};
        }
        pendingDragX = dx;
        if (!dragScheduled) {
            dragScheduled = true;
            dragFrame = window.requestAnimationFrame(paintDrag);
        }
    };
    const touchFinish = (event, cancelled = false) => {
        if (!touchStart) return;
        const touch = Array.from(event.changedTouches).find((item) => item.identifier === touchStart.id);
        if (!touch && !cancelled) return;
        const dx = touch ? touch.clientX - touchStart.x : pendingDragX;
        const dy = touch ? touch.clientY - touchStart.y : 0;
        const elapsed = Date.now() - touchStart.at;
        if (!drag) {
            if (!cancelled && touch) swipeFrom(touchStart, touch.clientX, touch.clientY);
            touchStart = null;
            return;
        }
        dragScheduled = false;
        if (dragFrame) window.cancelAnimationFrame(dragFrame);
        dragFrame = 0;
        const progress = Math.max(0, Math.min(1, -drag.direction * dx / drag.width));
        setProgress(drag.direction, progress);
        const commit = !cancelled && Math.abs(dx) > Math.abs(dy) * 1.25
            && (progress >= 0.22 || (Math.abs(dx) >= 45 && elapsed < 300));
        const {index, direction, imageUrl, token} = drag;
        drag = null;
        touchStart = null;
        hero.classList.remove('is-dragging');
        const duration = Math.max(120, Math.round(slideDuration * (commit ? 1 - progress : progress)));
        let settled = false;
        const settle = () => {
            if (settled || token !== motionToken) return;
            settled = true;
            settleSlide(index, imageUrl, commit, token, direction, duration);
        };
        window.requestAnimationFrame(settle);
        window.setTimeout(settle, 24);
    };
    [zone, copy].forEach((surface) => {
        surface.addEventListener('touchstart', touchBegin, {passive: true});
        surface.addEventListener('touchmove', touchMove, {passive: true});
        surface.addEventListener('touchend', touchFinish, {passive: true});
        surface.addEventListener('touchcancel', (event) => touchFinish(event, true), {passive: true});
    });

    let pointerStart = null;
    const pointerTouch = (event) => ({
        target: event.target,
        changedTouches: [{identifier: event.pointerId, clientX: event.clientX, clientY: event.clientY}],
    });
    zone.addEventListener('pointerdown', (event) => {
        if (event.pointerType === 'touch') return;
        if (event.pointerType === 'mouse' && event.button !== 0) return;
        if (sliding) return;
        pointerStart = {id: event.pointerId, x: event.clientX, y: event.clientY};
        zone.setPointerCapture?.(event.pointerId);
        touchBegin(pointerTouch(event));
    });
    zone.addEventListener('pointermove', (event) => {
        if (!pointerStart || pointerStart.id !== event.pointerId) return;
        touchMove(pointerTouch(event));
    });
    zone.addEventListener('pointerup', (event) => {
        if (!pointerStart || pointerStart.id !== event.pointerId) return;
        const start = pointerStart;
        pointerStart = null;
        if (touchStart) touchFinish(pointerTouch(event));
        else swipeFrom(start, event.clientX, event.clientY);
        if (zone.hasPointerCapture?.(event.pointerId)) zone.releasePointerCapture(event.pointerId);
    });
    zone.addEventListener('pointercancel', (event) => {
        if (touchStart) touchFinish(pointerTouch(event), true);
        pointerStart = null;
    });
    zone.addEventListener('keydown', (event) => {
        if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
        event.preventDefault();
        move(event.key === 'ArrowRight' ? 1 : -1);
    });
    [-1, 1].forEach((direction) => {
        find(`[data-hero-direction="${direction}"]`)?.addEventListener('click', () => move(direction));
    });
    dots.forEach((dot, index) => dot.addEventListener('click', () => selectSlide(index)));
    window.setTimeout(() => warmNeighbors(0), 0);
})();
