(function () {
    const ABS = {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
    };
    const UNITS = [
        ['year', 31536000],
        ['month', 2592000],
        ['week', 604800],
        ['day', 86400],
        ['hour', 3600],
        ['minute', 60],
        ['second', 1],
    ];
    const rtf = typeof Intl !== 'undefined' && Intl.RelativeTimeFormat
        ? new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' })
        : null;

    function relative(date) {
        const secs = (date.getTime() - Date.now()) / 1000;
        const abs = Math.abs(secs);
        if (abs < 30) return 'just now';
        if (!rtf) return date.toLocaleString();
        for (const [unit, s] of UNITS) {
            if (abs >= s || unit === 'second') {
                return rtf.format(Math.round(secs / s), unit);
            }
        }
        return date.toLocaleString();
    }

    function renderRelativeTimes(root) {
        (root || document).querySelectorAll('time.rel').forEach((el) => {
            const iso = el.getAttribute('datetime');
            const d = new Date(iso);
            if (Number.isNaN(d.getTime())) return;
            el.textContent = `${d.toLocaleString(undefined, ABS)} (${relative(d)})`;
            el.setAttribute('data-tip', iso);
            el.removeAttribute('title');
        });
    }

    function ensureToastHost() {
        let host = document.getElementById('ui-toast-host');
        if (host) return host;
        host = document.createElement('div');
        host.id = 'ui-toast-host';
        host.className = 'ui-toast-host';
        host.setAttribute('aria-live', 'polite');
        host.setAttribute('aria-relevant', 'additions');
        document.body.appendChild(host);
        return host;
    }

    function towerToast(message, options) {
        const opts = options || {};
        const host = ensureToastHost();
        const toast = document.createElement('div');
        toast.className = 'ui-toast';
        if (opts.type === 'error') {
            toast.classList.add('is-error');
            toast.setAttribute('role', 'alert');
        } else if (opts.type === 'success') {
            toast.classList.add('is-success');
        }
        toast.textContent = message || '';
        host.appendChild(toast);
        const ttl = typeof opts.duration === 'number' ? opts.duration : 4200;
        window.setTimeout(() => {
            toast.remove();
            if (!host.children.length) host.remove();
        }, ttl);
        return toast;
    }

    function towerConfirm(message, options) {
        const opts = options || {};
        return new Promise((resolve) => {
            const backdrop = document.createElement('div');
            backdrop.className = 'ui-dialog-backdrop';

            const dialog = document.createElement('div');
            dialog.className = 'ui-dialog';
            dialog.setAttribute('role', 'dialog');
            dialog.setAttribute('aria-modal', 'true');
            dialog.setAttribute('aria-labelledby', 'ui-dialog-title');

            const title = document.createElement('h2');
            title.className = 'ui-dialog-title';
            title.id = 'ui-dialog-title';
            title.textContent = opts.title || 'Please confirm';

            const body = document.createElement('p');
            body.className = 'ui-dialog-body';
            body.textContent = message || '';

            const actions = document.createElement('div');
            actions.className = 'ui-dialog-actions';

            const cancel = document.createElement('button');
            cancel.type = 'button';
            cancel.className = 'btn btn-secondary';
            cancel.textContent = opts.cancelLabel || 'Cancel';

            const confirm = document.createElement('button');
            confirm.type = 'button';
            confirm.className = opts.danger ? 'btn btn-danger' : 'btn';
            confirm.textContent = opts.confirmLabel || 'Confirm';

            actions.append(cancel, confirm);
            dialog.append(title, body, actions);
            backdrop.appendChild(dialog);

            const previous = document.activeElement;
            function finish(result) {
                backdrop.remove();
                document.removeEventListener('keydown', onKey);
                if (previous && typeof previous.focus === 'function') previous.focus();
                resolve(result);
            }
            function onKey(event) {
                if (event.key === 'Escape') {
                    event.preventDefault();
                    finish(false);
                }
            }
            backdrop.addEventListener('click', (event) => {
                if (event.target === backdrop) finish(false);
            });
            cancel.addEventListener('click', () => finish(false));
            confirm.addEventListener('click', () => finish(true));
            document.addEventListener('keydown', onKey);
            document.body.appendChild(backdrop);
            confirm.focus();
        });
    }

    function setNavOpen(open) {
        const sidebar = document.querySelector('.site-sidebar');
        const backdrop = document.getElementById('site-nav-backdrop');
        const btn = document.getElementById('site-menu-btn');
        if (!sidebar) return;
        sidebar.classList.toggle('is-open', open);
        document.body.classList.toggle('nav-open', open);
        if (backdrop) backdrop.classList.toggle('is-open', open);
        if (btn) {
            btn.setAttribute('aria-expanded', open ? 'true' : 'false');
            btn.setAttribute('aria-label', open ? 'Close navigation' : 'Open navigation');
        }
    }

    function initNav() {
        const btn = document.getElementById('site-menu-btn');
        const backdrop = document.getElementById('site-nav-backdrop');
        const sidebar = document.querySelector('.site-sidebar');
        if (!btn || !sidebar) return;
        btn.addEventListener('click', () => {
            setNavOpen(!sidebar.classList.contains('is-open'));
        });
        if (backdrop) {
            backdrop.addEventListener('click', () => setNavOpen(false));
        }
        sidebar.querySelectorAll('a').forEach((link) => {
            link.addEventListener('click', () => setNavOpen(false));
        });
        document.addEventListener('keydown', (event) => {
            if (event.key === 'Escape') setNavOpen(false);
        });
        window.addEventListener('resize', () => {
            if (window.innerWidth > 640) setNavOpen(false);
        });
    }

    function initConfirmForms() {
        document.addEventListener('submit', async (event) => {
            const form = event.target;
            if (!(form instanceof HTMLFormElement)) return;
            const message = form.getAttribute('data-confirm');
            if (!message || form.dataset.confirmAccepted === '1') return;
            event.preventDefault();
            const ok = await towerConfirm(message, {
                title: form.getAttribute('data-confirm-title') || 'Please confirm',
                confirmLabel: form.getAttribute('data-confirm-label') || 'Confirm',
                danger: form.hasAttribute('data-confirm-danger'),
            });
            if (!ok) return;
            form.dataset.confirmAccepted = '1';
            form.requestSubmit();
        });
    }

    window.towerToast = towerToast;
    window.towerConfirm = towerConfirm;
    window.renderRelativeTimes = renderRelativeTimes;

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => {
            initNav();
            initConfirmForms();
            renderRelativeTimes();
            window.setInterval(renderRelativeTimes, 30000);
        });
    } else {
        initNav();
        initConfirmForms();
        renderRelativeTimes();
        window.setInterval(renderRelativeTimes, 30000);
    }
})();
