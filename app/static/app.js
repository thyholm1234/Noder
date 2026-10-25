// Noder — client-side enhancements. Everything degrades gracefully without JS
// except CSRF protection, which needs the token injected below.
(function () {
    'use strict';

    const $ = (sel, root = document) => root.querySelector(sel);
    const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
    const store = {
        get(key, fallback = null) { try { return localStorage.getItem('noder.' + key) ?? fallback; } catch (e) { return fallback; } },
        set(key, value) { try { value === null ? localStorage.removeItem('noder.' + key) : localStorage.setItem('noder.' + key, value); } catch (e) { /* private mode */ } },
    };
    const norm = (s) => (s || '').toString().toLowerCase().normalize('NFKD').replace(/[̀-ͯ]/g, '').trim();

    // ------------------------------------------------------------------
    // CSRF: add the session token to every POST form and fetch call
    // ------------------------------------------------------------------
    const csrf = $('meta[name="csrf-token"]')?.content;
    function ensureToken(form) {
        if (!csrf || (form.method || '').toLowerCase() !== 'post' || form.querySelector('input[name="csrf_token"]')) return;
        const input = document.createElement('input');
        input.type = 'hidden';
        input.name = 'csrf_token';
        input.value = csrf;
        form.appendChild(input);
    }
    $$('form').forEach(ensureToken);
    document.addEventListener('submit', (e) => ensureToken(e.target), true);

    window.noderPost = function (url, data) {
        const isJson = !(data instanceof URLSearchParams);
        return fetch(url, {
            method: 'POST',
            headers: { 'X-CSRF-Token': csrf || '', 'Accept': 'application/json',
                       'Content-Type': isJson ? 'application/json' : 'application/x-www-form-urlencoded' },
            body: isJson ? JSON.stringify(data) : data,
            credentials: 'same-origin',
        }).then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); });
    };

    // ------------------------------------------------------------------
    // Toasts
    // ------------------------------------------------------------------
    const stack = $('#toasts');
    function dismiss(toast) {
        toast.classList.add('toast--leaving');
        setTimeout(() => toast.remove(), 250);
    }
    function arm(toast) {
        toast.querySelector('.toast-close')?.addEventListener('click', () => dismiss(toast));
        if (!toast.hasAttribute('data-sticky')) {
            const ms = toast.classList.contains('toast--error') ? 9000 : 4500;
            setTimeout(() => dismiss(toast), ms);
        }
    }
    window.toast = function (message, kind = 'success') {
        if (!stack) return;
        const el = document.createElement('div');
        el.className = 'toast toast--' + kind;
        el.innerHTML = '<div class="toast-body"></div><button type="button" class="toast-close" aria-label="Luk">×</button>';
        el.firstChild.textContent = message;
        stack.appendChild(el);
        arm(el);
    };
    $$('.toast').forEach(arm);

    // ------------------------------------------------------------------
    // Confirm dialog for destructive forms: <form data-confirm="…">
    // ------------------------------------------------------------------
    const dialog = $('#confirm-dialog');
    document.addEventListener('submit', (e) => {
        const form = e.target;
        if (!form.dataset.confirm || form.dataset.confirmed) return;
        e.preventDefault();
        const submitter = e.submitter;
        if (!dialog || !dialog.showModal) {
            if (window.confirm(form.dataset.confirm)) { form.dataset.confirmed = '1'; form.requestSubmit(submitter); }
            return;
        }
        $('#confirm-message').textContent = form.dataset.confirm;
        $('#confirm-ok').textContent = form.dataset.confirmOk || 'Ja, slet';
        dialog.returnValue = '';
        dialog.showModal();
        dialog.addEventListener('close', function onClose() {
            dialog.removeEventListener('close', onClose);
            if (dialog.returnValue === 'ok') {
                form.dataset.confirmed = '1';
                form.requestSubmit(submitter && form.contains(submitter) ? submitter : undefined);
            }
        });
    });

    // Disable submit buttons while a form is in flight (prevents double posts)
    document.addEventListener('submit', (e) => {
        const form = e.target;
        if (e.defaultPrevented || form.method.toLowerCase() !== 'post') return;
        setTimeout(() => $$('button[type="submit"], button:not([type])', form).forEach((b) => { b.disabled = true; }), 0);
        if (form.matches('[data-upload-form]')) $('[data-upload-progress]', form)?.removeAttribute('hidden');
    });
    window.addEventListener('pageshow', () => $$('form button[disabled]').forEach((b) => { b.disabled = false; }));

    // ------------------------------------------------------------------
    // Small declarative helpers
    // ------------------------------------------------------------------
    document.addEventListener('click', (e) => {
        const toggle = e.target.closest('[data-toggle]');
        if (toggle) {
            const target = $(toggle.dataset.toggle);
            if (target) {
                target.hidden = !target.hidden;
                if (!target.hidden) (target.querySelector('[data-autofocus], input:not([type=hidden]), textarea, select'))?.focus();
            }
        }
        const cls = e.target.closest('[data-toggle-class]');
        if (cls) $(cls.dataset.target)?.classList.toggle(cls.dataset.toggleClass);

        const opener = e.target.closest('[data-open-dialog]');
        if (opener) $(opener.dataset.openDialog)?.showModal();
        const closer = e.target.closest('[data-close-dialog]');
        if (closer) closer.closest('dialog')?.close();

        const copy = e.target.closest('[data-copy-text]');
        if (copy) {
            navigator.clipboard?.writeText(copy.dataset.copyText.trim()).then(
                () => toast('Kopieret til udklipsholderen'),
                () => toast('Kunne ikke kopiere', 'error'));
        }

        const reveal = e.target.closest('[data-reveal]');
        if (reveal) {
            const input = $(reveal.dataset.reveal);
            input.type = input.type === 'password' ? 'text' : 'password';
            input.focus();
        }

        const fs = e.target.closest('[data-fullscreen]');
        if (fs) {
            const el = $(fs.dataset.fullscreen);
            (el.requestFullscreen || el.webkitRequestFullscreen)?.call(el);
        }
    });

    // Close dialogs by clicking the backdrop
    $$('dialog.modal').forEach((d) => d.addEventListener('click', (e) => { if (e.target === d) d.close(); }));

    // Password confirmation fields
    $$('[data-match]').forEach((input) => {
        const other = $(input.dataset.match);
        const check = () => input.setCustomValidity(input.value && input.value !== other.value ? input.dataset.matchMessage : '');
        input.addEventListener('input', check);
        other.addEventListener('input', check);
    });

    // Warn before leaving a form with unsaved edits
    $$('form[data-dirty-guard]').forEach((form) => {
        let dirty = false;
        form.addEventListener('input', () => { dirty = true; });
        form.addEventListener('submit', () => { dirty = false; });
        window.addEventListener('beforeunload', (e) => { if (dirty) { e.preventDefault(); e.returnValue = ''; } });
    });

    // Live filtering: <input data-filter=".items"> hides items whose data-search doesn't match
    $$('[data-filter]').forEach((input) => {
        const items = () => $$(input.dataset.filter);
        const empty = input.closest('section, form')?.querySelector('[data-filter-empty]');
        const run = () => {
            const terms = norm(input.value).split(/\s+/).filter(Boolean);
            let shown = 0;
            items().forEach((el) => {
                const hay = norm(el.dataset.search || el.textContent);
                const ok = terms.every((t) => hay.includes(t));
                el.hidden = !ok;
                if (ok) shown++;
            });
            if (empty) empty.hidden = shown > 0;
        };
        input.addEventListener('input', run);
    });

    // "Tildel 3 valgte" — live count on multi-select pickers. Counts checkboxes outside the form too
    // (form="…" attribute); data-count-name limits it to one field, data-count-autohide hides the form at 0.
    $$('[data-count-label]').forEach((btn) => {
        const form = btn.form;
        const base = btn.textContent;
        const name = btn.dataset.countName;
        const update = () => {
            const n = [...form.elements].filter((el) => el.type === 'checkbox' && el.checked && (!name || el.name === name)).length;
            btn.textContent = n ? btn.dataset.countLabel.replace('{n}', n) : base;
            btn.disabled = n === 0;
            if ('countAutohide' in btn.dataset) form.hidden = n === 0;
        };
        document.addEventListener('change', update);
        update();
    });

    // "Select all" checkbox: <input type=checkbox data-check-all="selector">
    $$('[data-check-all]').forEach((all) => {
        const boxes = () => $$(all.dataset.checkAll);
        all.addEventListener('change', () => {
            boxes().forEach((b) => { if (!b.closest('[hidden]')) b.checked = all.checked; });
        });
        document.addEventListener('change', (e) => {
            if (e.target === all || !boxes().includes(e.target)) return;
            const checked = boxes().filter((b) => b.checked).length;
            all.checked = checked === boxes().length;
            all.indeterminate = checked > 0 && !all.checked;
        });
    });

    // Keyboard: "/" focuses search; ←/→ follow data-hotkey links; space toggles the active audio
    document.addEventListener('keydown', (e) => {
        const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName) || document.activeElement?.isContentEditable;
        if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
        if (e.key === '/') {
            const search = $('[data-search-shortcut]');
            if (search) { e.preventDefault(); search.focus(); search.select(); }
        }
        const link = $(`[data-hotkey="${e.key}"]`);
        if (link) { e.preventDefault(); link.click(); }
        if (e.key === ' ' && window.noderActiveAudio) {
            e.preventDefault();
            const a = window.noderActiveAudio;
            a.paused ? a.play() : a.pause();
        }
    });

    // ------------------------------------------------------------------
    // Theme
    // ------------------------------------------------------------------
    $$('[data-theme-toggle]').forEach((btn) => btn.addEventListener('click', () => {
        const root = document.documentElement;
        const systemDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
        const current = root.dataset.theme || (systemDark ? 'dark' : 'light');
        const next = current === 'dark' ? 'light' : 'dark';
        root.dataset.theme = next;
        store.set('theme', next);
    }));

    // ------------------------------------------------------------------
    // Tabs (lazy-loads PDF iframes on first view)
    // ------------------------------------------------------------------
    const INLINE_PDF = '(pointer: fine) and (min-width: 769px)';
    function loadVisiblePdfs() {
        if (!window.matchMedia(INLINE_PDF).matches) return;
        $$('.tab-panel--active iframe[data-src]').forEach((frame) => {
            frame.src = frame.dataset.src;
            frame.removeAttribute('data-src');
        });
    }
    function activateTab(btn) {
        const tabs = btn.closest('[data-tabs]');
        $$('.tab-btn', tabs).forEach((b) => b.classList.toggle('tab-btn--active', b === btn));
        $$('.tab-panel', tabs).forEach((p) => {
            const on = p.id === btn.dataset.tab;
            p.classList.toggle('tab-panel--active', on);
        });
        loadVisiblePdfs();
    }
    document.addEventListener('click', (e) => {
        const btn = e.target.closest('[data-tabs] .tab-btn');
        if (btn) activateTab(btn);
    });

    // ------------------------------------------------------------------
    // "Min stemme" — preferred voice, remembered per device
    // ------------------------------------------------------------------
    const voiceMatches = (name, voice) => {
        const n = norm(name), v = norm(voice);
        return !!(n && v) && (n === v || n.startsWith(v) || n.includes(v));
    };
    function applyVoice() {
        const voice = store.get('voice', '');
        $$('[data-voice]').forEach((el) => el.classList.toggle('is-mine', voiceMatches(el.dataset.voice, voice)));
        // Put my audio part first
        $$('[data-voice-sort]').forEach((list) => {
            const mine = $$(':scope > .is-mine', list);
            mine.reverse().forEach((el) => list.prepend(el));
        });
        // Open my PDF tab
        $$('[data-tabs]').forEach((tabs) => {
            const btn = $('.tab-btn.is-mine', tabs);
            if (btn) activateTab(btn);
        });
        // Exports default to my part
        $$('select[data-voice-default]').forEach((sel) => {
            const opt = Array.from(sel.options).find((o) => voiceMatches(o.value, voice));
            if (opt) sel.value = opt.value;
        });
    }
    $$('[data-voice-picker]').forEach((sel) => {
        // De-duplicate options and include the remembered voice
        const seen = new Set();
        Array.from(sel.options).forEach((o) => {
            const key = norm(o.value) || '__none' + o.index;
            if (seen.has(key)) o.remove(); else seen.add(key);
        });
        const voice = store.get('voice', '');
        const match = Array.from(sel.options).find((o) => o.value && voiceMatches(o.value, voice));
        if (match) sel.value = match.value;
        else if (voice && !sel.hasAttribute('data-player-voice')) {
            sel.add(new Option(voice, voice), 1);
            sel.value = voice;
        }
        sel.addEventListener('change', () => {
            if (sel.value) store.set('voice', sel.value); else if (!sel.hasAttribute('data-player-voice')) store.set('voice', null);
            applyVoice();
            sel.dispatchEvent(new CustomEvent('voicechange', { bubbles: true }));
        });
    });
    $$('[data-voice-input]').forEach((input) => {
        input.value = store.get('voice', '');
        input.addEventListener('change', () => {
            store.set('voice', input.value.trim() || null);
            toast(input.value.trim() ? `Din stemme er sat til “${input.value.trim()}”` : 'Stemmevalg fjernet');
        });
    });
    applyVoice();

    // ------------------------------------------------------------------
    // Audio: tempo, ±5 s, A-B loop, one-at-a-time playback
    // ------------------------------------------------------------------
    const RATES = [0.6, 0.75, 0.9, 1, 1.1];
    window.noderActiveAudio = null;
    document.addEventListener('play', (e) => {
        if (!(e.target instanceof HTMLMediaElement)) return;
        $$('audio').forEach((a) => { if (a !== e.target) a.pause(); });
        window.noderActiveAudio = e.target;
    }, true);

    $$('audio[data-enhance]').forEach((audio) => {
        const bar = document.createElement('div');
        bar.className = 'audio-tools';
        bar.innerHTML = `
            <button type="button" class="btn btn-sm btn-ghost" data-act="back" title="5 sek. tilbage">−5s</button>
            <button type="button" class="btn btn-sm btn-ghost" data-act="fwd" title="5 sek. frem">+5s</button>
            <span class="audio-tools-sep"></span>
            <span class="audio-tools-label">Tempo</span>
            <div class="segmented segmented--sm">${RATES.map((r) => `<button type="button" data-rate="${r}">${Math.round(r * 100)}%</button>`).join('')}</div>
            <span class="audio-tools-sep"></span>
            <button type="button" class="btn btn-sm btn-ghost" data-act="loop" title="Sæt A og B for at gentage et stykke">A–B</button>`;
        audio.after(bar);

        const rateButtons = $$('[data-rate]', bar);
        const setRate = (r) => {
            audio.playbackRate = r;
            audio.preservesPitch = true;
            rateButtons.forEach((b) => b.classList.toggle('active', +b.dataset.rate === r));
        };
        setRate(parseFloat(store.get('rate', '1')) || 1);
        audio.addEventListener('loadedmetadata', () => { audio.playbackRate = parseFloat(store.get('rate', '1')) || 1; });

        let a = null, b = null;
        const loopBtn = $('[data-act="loop"]', bar);
        const showLoop = () => {
            const fmt = (t) => `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, '0')}`;
            loopBtn.textContent = a === null ? 'A–B' : b === null ? `A ${fmt(a)} → B?` : `↻ ${fmt(a)}–${fmt(b)}`;
            loopBtn.classList.toggle('active', a !== null);
        };
        audio.addEventListener('timeupdate', () => {
            if (a !== null && b !== null && audio.currentTime >= b) audio.currentTime = a;
        });

        bar.addEventListener('click', (e) => {
            const btn = e.target.closest('button');
            if (!btn) return;
            if (btn.dataset.rate) { setRate(+btn.dataset.rate); store.set('rate', btn.dataset.rate); }
            if (btn.dataset.act === 'back') audio.currentTime = Math.max(0, audio.currentTime - 5);
            if (btn.dataset.act === 'fwd') audio.currentTime = Math.min(audio.duration || 0, audio.currentTime + 5);
            if (btn.dataset.act === 'loop') {
                if (a === null) a = audio.currentTime;
                else if (b === null) { b = audio.currentTime > a ? audio.currentTime : null; if (b !== null) { audio.currentTime = a; audio.play(); } }
                else { a = b = null; }
                showLoop();
            }
        });
    });

    // ------------------------------------------------------------------
    // Upload dropzone
    // ------------------------------------------------------------------
    $$('[data-dropzone]').forEach((zone) => {
        const input = $('input[type=file]', zone);
        const list = $('[data-dropzone-files]', zone);
        const render = () => {
            list.innerHTML = '';
            Array.from(input.files).forEach((f) => {
                const li = document.createElement('li');
                const mb = (f.size / 1024 / 1024).toFixed(1);
                li.textContent = `${f.name} · ${mb} MB`;
                list.appendChild(li);
            });
            zone.classList.toggle('has-files', input.files.length > 0);
        };
        ['dragenter', 'dragover'].forEach((ev) => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add('is-over'); }));
        ['dragleave', 'drop'].forEach((ev) => zone.addEventListener(ev, () => zone.classList.remove('is-over')));
        zone.addEventListener('drop', (e) => {
            e.preventDefault();
            if (e.dataTransfer.files.length) { input.files = e.dataTransfer.files; render(); }
        });
        input.addEventListener('change', render);
    });

    // ------------------------------------------------------------------
    // Setlists: drag to reorder, click separators to rename
    // ------------------------------------------------------------------
    function renumber(list) {
        let n = 0;
        $$('.setlist-item-card', list).forEach((el) => {
            const num = $('.setlist-item-num', el);
            if (num) num.textContent = ++n;
        });
    }
    function saveOrder(list) {
        renumber(list);
        const order = $$('[data-item-id]', list).map((el) => el.dataset.itemId);
        noderPost(list.dataset.sortableUrl, { order })
            .then(() => toast('Rækkefølge gemt'))
            .catch(() => toast('Kunne ikke gemme rækkefølgen — genindlæs siden', 'error'));
    }
    // Sortable.min.js is loaded after this file, so wait until every script has run.
    // Drag handles work with mouse, touch and pen; the arrow buttons work everywhere (keyboard, screen readers, no Sortable).
    function initSetlists() {
        $$('[data-sortable-url]').forEach((list) => {
            renumber(list);
            if (window.Sortable) {
                new Sortable(list, {
                    animation: 160,
                    handle: '.drag-handle',
                    ghostClass: 'is-ghost',
                    fallbackTolerance: 3,
                    delayOnTouchOnly: true,
                    touchStartThreshold: 3,
                    onEnd: () => saveOrder(list),
                });
            }
            list.addEventListener('click', (e) => {
                const btn = e.target.closest('[data-move]');
                if (!btn) return;
                const item = btn.closest('.setlist-item-card');
                const dir = btn.dataset.move === 'up' ? -1 : 1;
                const other = dir < 0 ? item.previousElementSibling : item.nextElementSibling;
                if (!other) return;
                dir < 0 ? list.insertBefore(item, other) : list.insertBefore(other, item);
                saveOrder(list);
                btn.focus();
            });
        });
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initSetlists);
    else initSetlists();

    // ------------------------------------------------------------------
    // Choir page: one panel at a time, driven by the URL hash (#songs, #setlist-3, …).
    // Without JS every panel simply stays visible.
    // ------------------------------------------------------------------
    const panelNav = $('[data-panel-nav]');
    if (panelNav) {
        const panels = $$('[data-panel]');
        const links = $$('a[href^="#"]', panelNav);
        const showPanel = () => {
            const id = decodeURIComponent(location.hash.slice(1));
            const target = id ? document.getElementById(id) : null;
            const panel = target?.closest('[data-panel]') || panels[0];
            panels.forEach((p) => { p.hidden = p !== panel; });
            links.forEach((a) => {
                const on = a.getAttribute('href') === '#' + panel.id;
                a.classList.toggle('active', on);
                on ? a.setAttribute('aria-current', 'page') : a.removeAttribute('aria-current');
            });
            if (target?.classList.contains('setlist-card')) {
                const body = $('.setlist-body', target);
                if (body) body.hidden = false;
            }
            if (target && target !== panel) target.scrollIntoView({ block: 'start' });
            else if (!location.hash || target === panel) window.scrollTo(0, 0);
        };
        window.addEventListener('hashchange', showPanel);
        showPanel();
    }

    $$('[data-rename-url]').forEach((el) => {
        let original = el.textContent.trim();
        el.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); el.blur(); }
            if (e.key === 'Escape') { el.textContent = original; el.blur(); }
        });
        el.addEventListener('paste', (e) => {
            e.preventDefault();
            document.execCommand('insertText', false, (e.clipboardData || window.clipboardData).getData('text').replace(/\s+/g, ' '));
        });
        el.addEventListener('blur', () => {
            const value = el.textContent.replace(/\s+/g, ' ').trim();
            el.textContent = value || original;
            if (!value || value === original) return;
            noderPost(el.dataset.renameUrl, new URLSearchParams({ title: value }))
                .then(() => { original = value; toast('Separator omdøbt'); })
                .catch(() => { el.textContent = original; toast('Kunne ikke gemme', 'error'); });
        });
    });

    // ------------------------------------------------------------------
    // PDF preview: only load iframes where they can actually render
    // ------------------------------------------------------------------
    loadVisiblePdfs();
    window.matchMedia(INLINE_PDF).addEventListener?.('change', loadVisiblePdfs);

    // ------------------------------------------------------------------
    // Mobile "Mere" sheet
    // ------------------------------------------------------------------
    const sidebar = $('#sidebar');
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') sidebar?.classList.remove('sidebar--open'); });
    $$('.sidebar-footer a').forEach((a) => a.addEventListener('click', () => sidebar.classList.remove('sidebar--open')));

    // ------------------------------------------------------------------
    // PWA: service worker + install button
    // ------------------------------------------------------------------
    if ('serviceWorker' in navigator && (location.protocol === 'https:' || location.hostname === 'localhost' || location.hostname === '127.0.0.1')) {
        window.addEventListener('load', () => navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => {}));
    }

    const standalone = window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
    const installBtns = $$('[data-install]');
    const isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
    let deferredPrompt = null;
    const showInstall = (on) => installBtns.forEach((b) => { b.hidden = !on || standalone; });

    window.addEventListener('beforeinstallprompt', (e) => {
        e.preventDefault();
        deferredPrompt = e;
        showInstall(true);
    });
    window.addEventListener('appinstalled', () => {
        deferredPrompt = null;
        showInstall(false);
        toast('Noder er installeret — find den på din hjemmeskærm');
    });
    // iOS has no install prompt: explain the Share → "Føj til hjemmeskærm" route instead
    if (isIOS && !standalone) showInstall(true);

    installBtns.forEach((btn) => btn.addEventListener('click', async () => {
        if (deferredPrompt) {
            deferredPrompt.prompt();
            const { outcome } = await deferredPrompt.userChoice;
            deferredPrompt = null;
            if (outcome === 'accepted') showInstall(false);
        } else if (isIOS) {
            toast('Tryk på Del-ikonet (firkant med pil) i Safari og vælg “Føj til hjemmeskærm”.', 'secret');
        }
    }));

    // Export dialog: toast while the server builds the PDF
    $$('[data-export-form]').forEach((form) => form.addEventListener('submit', (e) => {
        if (e.submitter?.value === '1') toast('Samler PDF… download starter om et øjeblik');
        form.closest('dialog')?.close();
    }));
})();
