    function resetReaderEntryButtons() {
        const readerEntryButtons = document.querySelectorAll('[data-reader-entry]');

        readerEntryButtons.forEach(button => {
            button.dataset.loading = 'false';
            button.classList.remove('is-loading');
            button.removeAttribute('aria-busy');
        });
    }

    function resetDetailSubmitButtons() {
        document.querySelectorAll('.comic-detail form button.is-loading').forEach(button => {
            button.classList.remove('is-loading');
            button.removeAttribute('aria-busy');
        });
    }

    function bindComicSynopsisToggle() {
        const description = document.getElementById('comic-detail-description');
        const toggle = document.querySelector('[data-comic-synopsis-toggle]');
        if (!description || !toggle || toggle.dataset.detailBound === 'true') return;
        toggle.dataset.detailBound = 'true';

        const syncOverflow = () => {
            if (!description.classList.contains('is-expanded')) {
                toggle.hidden = description.scrollHeight <= description.clientHeight + 1;
            }
        };

        toggle.addEventListener('click', () => {
            const expanded = description.classList.toggle('is-expanded');
            toggle.setAttribute('aria-expanded', String(expanded));
            toggle.textContent = expanded ? '收起简介' : '展开简介';
            if (!expanded) window.requestAnimationFrame(syncOverflow);
        });

        window.requestAnimationFrame(syncOverflow);
        if (document.fonts && document.fonts.ready) document.fonts.ready.then(syncOverflow);

    }

    function bindReaderEntryButtons() {
        const readerEntryButtons = document.querySelectorAll('[data-reader-entry]');

        readerEntryButtons.forEach(button => {
            if (button.dataset.bound === 'true') {
                return;
            }

            button.dataset.bound = 'true';
            button.addEventListener('click', (event) => {
                if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) {
                    return;
                }

                if (button.dataset.loading === 'true') {
                    event.preventDefault();
                    return;
                }

                event.preventDefault();
                button.dataset.loading = 'true';
                button.classList.add('is-loading');
                button.setAttribute('aria-busy', 'true');

                const targetUrl = button.getAttribute('href');
                window.requestAnimationFrame(() => {
                    window.location.href = targetUrl;
                });
            });
        });
    }

    function toggleCoverEditor() {
        const editor = document.getElementById('cover-editor-panel');
        const toggle = document.querySelector('[data-cover-editor-toggle]');
        if (!editor) {
            return;
        }

        if (editor.hasAttribute('hidden')) {
            editor.removeAttribute('hidden');
            if (toggle) {
                toggle.setAttribute('aria-expanded', 'true');
            }
            return;
        }

        editor.setAttribute('hidden', '');
        if (toggle) {
            toggle.setAttribute('aria-expanded', 'false');
        }
    }

    function toggleDetailOverflowMenu(forceClose = false) {
        const menu = document.getElementById('detail-overflow-menu');
        const toggle = document.querySelector('[data-detail-menu-toggle]');
        if (!menu || !toggle) {
            return;
        }

        const shouldOpen = forceClose ? false : menu.hasAttribute('hidden');
        if (shouldOpen) {
            menu.removeAttribute('hidden');
            toggle.setAttribute('aria-expanded', 'true');
            return;
        }

        menu.setAttribute('hidden', '');
        toggle.setAttribute('aria-expanded', 'false');
    }

    function searchChapters() {
        const searchInput = document.getElementById('chapter-search');
        const searchTerm = searchInput.value.toLowerCase();
        const chapterItems = document.querySelectorAll('.chapter-item');

        chapterItems.forEach(item => {
            const titleEl = item.querySelector('.chapter-title');
            const chapterTitle = (titleEl ? titleEl.textContent : '').toLowerCase();
            if (chapterTitle.includes(searchTerm)) {
                item.style.display = 'flex';
            } else {
                item.style.display = 'none';
            }
        });
    }

    function sortChapters() {
        const sortSelect = document.getElementById('chapter-sort');
        const sortType = sortSelect.value;
        const chapterList = document.getElementById('chapter-list');
        const chapterItems = Array.from(chapterList.querySelectorAll('.chapter-item'));

        if (sortType === 'asc') {
            chapterItems.sort((a, b) => {
                const aNum = parseInt(a.dataset.chapter);
                const bNum = parseInt(b.dataset.chapter);
                return aNum - bNum;
            });
        } else {
            chapterItems.sort((a, b) => {
                const aNum = parseInt(a.dataset.chapter);
                const bNum = parseInt(b.dataset.chapter);
                return bNum - aNum;
            });
        }

        chapterItems.forEach(item => {
            chapterList.appendChild(item);
        });
    }

    function initializeComicDetail() {
        resetReaderEntryButtons();
        resetDetailSubmitButtons();
        bindReaderEntryButtons();
        bindComicSynopsisToggle();
        document.querySelectorAll('.comic-detail form:not([data-api-v1])').forEach(form => {
            if (form.dataset.detailBound === 'true') return;
            form.dataset.detailBound = 'true';
            form.addEventListener('submit', event => {
                if (event.defaultPrevented) {
                    return;
                }
                if (event.submitter) {
                    event.submitter.classList.add('is-loading');
                    event.submitter.setAttribute('aria-busy', 'true');
                }
                const loader = document.getElementById('app-shell-loader');
                const status = document.getElementById('app-shell-loader-status');
                if (status) {
                    status.textContent = '正在提交操作…';
                }
                document.body.classList.remove('app-ready');
                if (loader) {
                    loader.classList.add('is-visible');
                }
            });
        });
    }
    document.addEventListener('DOMContentLoaded', initializeComicDetail);
    document.addEventListener('click', function(event) {
            const groupPicker = document.querySelector('.md-detail-group-picker');
            if (groupPicker && !groupPicker.contains(event.target)) {
                groupPicker.open = false;
            }
            const menu = document.getElementById('detail-overflow-menu');
            const toggle = document.querySelector('[data-detail-menu-toggle]');
            if (!menu || !toggle || menu.hasAttribute('hidden')) {
                return;
            }

            if (menu.contains(event.target) || toggle.contains(event.target)) {
                return;
            }

            toggleDetailOverflowMenu(true);
        });
    window.addEventListener('pageshow', function() {
        resetReaderEntryButtons();
        resetDetailSubmitButtons();
    });

    function refreshComicSynopsisLayout() {
        const description = document.getElementById('comic-detail-description');
        const toggle = document.querySelector('[data-comic-synopsis-toggle]');
        if (description && toggle && !description.classList.contains('is-expanded')) {
            toggle.hidden = description.scrollHeight <= description.clientHeight + 1;
        }
    }
    let comicSynopsisResizeTimer;
    window.addEventListener('resize', () => {
        clearTimeout(comicSynopsisResizeTimer);
        comicSynopsisResizeTimer = setTimeout(refreshComicSynopsisLayout, 120);
    }, {passive: true});
