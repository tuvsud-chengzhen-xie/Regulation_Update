/*
 * TÜV SÜD CPS — Productivity Nudges (JS)
 * Reusable module: toast, auto-save, shortcuts overlay, progress, command palette
 *
 * USAGE:
 *   Include nudges.css in your <head>, then include this script.
 *   Call NudgeSystem.init({ ... }) with your app-specific config.
 *
 * STANDALONE API (also available without init):
 *   showToast(message, { type, duration })
 *   toggleShortcutsOverlay()
 *   toggleCommandPalette()
 */

(function () {
    'use strict';

    // ═══════════════════════════════════════════════════════════════
    // 1. TOAST NOTIFICATION SYSTEM
    // ═══════════════════════════════════════════════════════════════
    let _toastContainer = null;

    function ensureToastContainer() {
        if (_toastContainer && document.body.contains(_toastContainer)) return;
        _toastContainer = document.createElement('div');
        _toastContainer.className = 'toast-container';
        _toastContainer.setAttribute('aria-live', 'polite');
        document.body.appendChild(_toastContainer);
    }

    const TOAST_ICONS = {
        success: '<svg viewBox="0 0 18 18" fill="none" stroke="#22c55e" stroke-width="2" stroke-linecap="round"><circle cx="9" cy="9" r="7" opacity="0.15" fill="#22c55e" stroke="none"/><path d="M5.5 9.5l2.5 2.5 4.5-5"/></svg>',
        error:   '<svg viewBox="0 0 18 18" fill="none" stroke="#dc3545" stroke-width="2" stroke-linecap="round"><circle cx="9" cy="9" r="7" opacity="0.15" fill="#dc3545" stroke="none"/><path d="M6.5 6.5l5 5M11.5 6.5l-5 5"/></svg>',
        info:    '<svg viewBox="0 0 18 18" fill="none" stroke="#0066cc" stroke-width="2" stroke-linecap="round"><circle cx="9" cy="9" r="7" opacity="0.15" fill="#0066cc" stroke="none"/><path d="M9 8.5v3.5"/><circle cx="9" cy="6.5" r="0.5" fill="#0066cc"/></svg>',
        warn:    '<svg viewBox="0 0 18 18" fill="none" stroke="#f59e0b" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 3L2 15h14L9 3z" opacity="0.12" fill="#f59e0b" stroke="none"/><path d="M9 3L2 15h14L9 3z"/><path d="M9 8v3"/><circle cx="9" cy="13" r="0.5" fill="#f59e0b"/></svg>'
    };

    /**
     * Show a toast notification.
     * @param {string} message — text to display
     * @param {Object} opts
     * @param {'success'|'error'|'info'|'warn'} opts.type — severity (default: 'info')
     * @param {number} opts.duration — ms before auto-dismiss (default: 3500)
     */
    window.showToast = function (message, { type = 'info', duration = 3500 } = {}) {
        ensureToastContainer();

        const toast = document.createElement('div');
        toast.className = `toast toast-${type}`;
        toast.innerHTML = `
            <span class="toast-icon">${TOAST_ICONS[type] || TOAST_ICONS.info}</span>
            <span class="toast-body">${message}</span>
            <div class="toast-progress" style="animation-duration:${duration}ms"></div>
        `;
        _toastContainer.appendChild(toast);

        const timer = setTimeout(() => dismissToast(toast), duration);
        toast.addEventListener('click', () => { clearTimeout(timer); dismissToast(toast); });

        // Limit stack to 5
        while (_toastContainer.children.length > 5) {
            dismissToast(_toastContainer.firstChild);
        }
    };

    function dismissToast(toast) {
        if (!toast || !toast.parentNode) return;
        toast.classList.add('toast-exit');
        toast.addEventListener('animationend', () => toast.remove(), { once: true });
        // Safety fallback
        setTimeout(() => { if (toast.parentNode) toast.remove(); }, 400);
    }


    // ═══════════════════════════════════════════════════════════════
    // 2. KEYBOARD SHORTCUTS OVERLAY
    // ═══════════════════════════════════════════════════════════════
    let _shortcutsOverlay = null;
    let _shortcutsConfig = [];

    function buildShortcutsOverlay() {
        if (_shortcutsOverlay) return;

        _shortcutsOverlay = document.createElement('div');
        _shortcutsOverlay.className = 'shortcuts-overlay';
        _shortcutsOverlay.id = 'shortcutsOverlay';
        _shortcutsOverlay.addEventListener('click', (e) => {
            if (e.target === _shortcutsOverlay) toggleShortcutsOverlay();
        });

        const card = document.createElement('div');
        card.className = 'shortcuts-card';
        card.innerHTML = '<h3><svg viewBox="0 0 18 18" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><rect x="2" y="5" width="14" height="10" rx="1.5"/><path d="M5 8h1M8 8h2M12 8h1M5 11h8"/></svg> Keyboard Shortcuts</h3>';

        // Group shortcuts by section
        const groups = {};
        _shortcutsConfig.forEach(s => {
            const sec = s.section || 'General';
            if (!groups[sec]) groups[sec] = [];
            groups[sec].push(s);
        });

        Object.entries(groups).forEach(([section, items]) => {
            const secEl = document.createElement('div');
            secEl.className = 'shortcuts-section';
            secEl.innerHTML = `<h4>${section}</h4>`;
            items.forEach(item => {
                const row = document.createElement('div');
                row.className = 'shortcut-row';
                const keysHtml = item.keys.map(k => `<kbd>${k}</kbd>`).join('');
                row.innerHTML = `<span class="shortcut-label">${item.label}</span><span class="shortcut-keys">${keysHtml}</span>`;
                secEl.appendChild(row);
            });
            card.appendChild(secEl);
        });

        const footer = document.createElement('div');
        footer.className = 'shortcuts-footer';
        footer.textContent = 'Press ? or Esc to close';
        card.appendChild(footer);

        _shortcutsOverlay.appendChild(card);
        document.body.appendChild(_shortcutsOverlay);
    }

    window.toggleShortcutsOverlay = function () {
        buildShortcutsOverlay();
        _shortcutsOverlay.classList.toggle('visible');
    };


    // ═══════════════════════════════════════════════════════════════
    // 3. COMMAND PALETTE
    // ═══════════════════════════════════════════════════════════════
    let _cmdPalette = null;
    let _cmdInput = null;
    let _cmdResults = null;
    let _cmdCommands = [];
    let _cmdActiveIndex = -1;

    function buildCommandPalette() {
        if (_cmdPalette) return;

        const overlay = document.createElement('div');
        overlay.className = 'command-palette-overlay';
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay) toggleCommandPalette();
        });

        const palette = document.createElement('div');
        palette.className = 'command-palette';

        _cmdInput = document.createElement('input');
        _cmdInput.className = 'command-input';
        _cmdInput.placeholder = 'Type a command…';
        _cmdInput.addEventListener('input', filterCommands);
        _cmdInput.addEventListener('keydown', handleCmdKeydown);

        _cmdResults = document.createElement('div');
        _cmdResults.className = 'command-results';

        palette.appendChild(_cmdInput);
        palette.appendChild(_cmdResults);
        overlay.appendChild(palette);
        document.body.appendChild(overlay);
        _cmdPalette = overlay;
    }

    function filterCommands() {
        const query = _cmdInput.value.toLowerCase().trim();
        _cmdResults.innerHTML = '';
        _cmdActiveIndex = -1;

        const filtered = query
            ? _cmdCommands.filter(c => c.label.toLowerCase().includes(query) || (c.tags || '').toLowerCase().includes(query))
            : _cmdCommands;

        filtered.forEach((cmd, i) => {
            const item = document.createElement('div');
            item.className = 'command-item';
            item.dataset.index = i;

            const keysHtml = cmd.keys ? cmd.keys.map(k => `<kbd>${k}</kbd>`).join('') : '';
            item.innerHTML = `
                <span class="command-item-label">${cmd.label}</span>
                <span class="command-item-keys">${keysHtml}</span>
            `;
            item.addEventListener('click', () => {
                toggleCommandPalette();
                cmd.action();
            });
            _cmdResults.appendChild(item);
        });

        if (filtered.length > 0) {
            _cmdActiveIndex = 0;
            _cmdResults.children[0].classList.add('active');
        }
    }

    function handleCmdKeydown(e) {
        const items = _cmdResults.querySelectorAll('.command-item');
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            if (_cmdActiveIndex < items.length - 1) {
                items[_cmdActiveIndex]?.classList.remove('active');
                _cmdActiveIndex++;
                items[_cmdActiveIndex]?.classList.add('active');
                items[_cmdActiveIndex]?.scrollIntoView({ block: 'nearest' });
            }
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            if (_cmdActiveIndex > 0) {
                items[_cmdActiveIndex]?.classList.remove('active');
                _cmdActiveIndex--;
                items[_cmdActiveIndex]?.classList.add('active');
                items[_cmdActiveIndex]?.scrollIntoView({ block: 'nearest' });
            }
        } else if (e.key === 'Enter') {
            e.preventDefault();
            const active = items[_cmdActiveIndex];
            if (active) active.click();
        } else if (e.key === 'Escape') {
            toggleCommandPalette();
        }
    }

    window.toggleCommandPalette = function () {
        buildCommandPalette();
        const isOpen = _cmdPalette.classList.contains('visible');
        _cmdPalette.classList.toggle('visible');
        if (!isOpen) {
            _cmdInput.value = '';
            filterCommands();
            setTimeout(() => _cmdInput.focus(), 50);
        }
    };


    // ═══════════════════════════════════════════════════════════════
    // 4. INIT — wire up with app-specific config
    // ═══════════════════════════════════════════════════════════════

    /**
     * Initialize the nudge system.
     * @param {Object} config
     * @param {Array} config.shortcuts — [{key, mod, action, label, keys:['⌘','S'], section:'File'}]
     * @param {Array} config.commands  — [{label, action, keys:['⌘','K'], tags:'search find'}]
     */
    window.NudgeSystem = {
        init(config = {}) {
            if (config.shortcuts) {
                _shortcutsConfig = config.shortcuts;

                // Register global key handler
                document.addEventListener('keydown', (e) => {
                    const tag = e.target.tagName;
                    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') {
                        // Only allow Escape in inputs
                        if (e.key !== 'Escape') return;
                    }

                    for (const s of config.shortcuts) {
                        if (s.key !== e.key) continue;
                        const needsMod = s.mod === 'cmd';
                        const hasMod = e.metaKey || e.ctrlKey;
                        if (needsMod && !hasMod) continue;
                        if (!needsMod && hasMod) continue;
                        e.preventDefault();
                        s.action();
                        return;
                    }
                });
            }

            if (config.commands) {
                _cmdCommands = config.commands;
            }
        }
    };

})();
