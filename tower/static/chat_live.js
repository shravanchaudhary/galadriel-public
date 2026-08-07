/** Shared live Tower chat (overlay + Chats pane): stream, stop, hydrate. */
window.ChatLive = (function () {
    const RENDER_OPTS = {
        userClass: 'wfc-msg wfc-msg-user',
        assistantClass: 'wfc-msg wfc-msg-assistant',
        userTextClass: 'wfc-msg-text',
    };

    function escapeHtml(t) {
        return window.ChatRender ? ChatRender.escapeHtml(t) : String(t || '');
    }

    function hydrate(log, history) {
        if (!log || !window.ChatRender) return;
        log.innerHTML = '';
        ChatRender.hydrateHistory(log, history || [], RENDER_OPTS);
        log.scrollTop = log.scrollHeight;
    }

    function atBottom(log) {
        return log.scrollHeight - log.scrollTop - log.clientHeight < 40;
    }

    function toBottom(log) {
        log.scrollTop = log.scrollHeight;
    }

    function startAssistant(log) {
        const div = document.createElement('div');
        div.className = 'wfc-msg wfc-msg-assistant';
        const bodyEl = document.createElement('div');
        bodyEl.className = 'msg-body';
        const typing = document.createElement('span');
        typing.className = 'typing';
        typing.innerHTML = '<i></i><i></i><i></i>';
        bodyEl.appendChild(typing);
        div.appendChild(bodyEl);
        log.appendChild(div);
        return { div, bodyEl, textEl: null, thoughtEl: null, toolCard: null, finished: false };
    }

    function clearTyping(turn) {
        if (!turn) return;
        const t = turn.bodyEl && turn.bodyEl.querySelector('.typing');
        if (t) t.remove();
    }

    function ensureTyping(turn) {
        if (!turn || turn.finished) return;
        let t = turn.bodyEl && turn.bodyEl.querySelector('.typing');
        if (!t) {
            t = document.createElement('span');
            t.className = 'typing';
            t.innerHTML = '<i></i><i></i><i></i>';
            turn.bodyEl.appendChild(t);
        } else {
            turn.bodyEl.appendChild(t);
        }
    }

    function appendText(delta, turn, log) {
        if (!turn) turn = startAssistant(log);
        if (!turn.textEl) {
            turn.textEl = document.createElement('div');
            turn.textEl.className = 'msg-text markdown-body';
            turn.bodyEl.appendChild(turn.textEl);
        }
        ChatRender.appendMarkdown(turn.textEl, delta);
        ensureTyping(turn);
        return turn;
    }

    function appendThought(delta, turn, opts) {
        const isNudge = opts && (opts.kind === 'nudge' || opts.is_nudge);
        if (!turn.thoughtEl || isNudge) {
            const d = document.createElement('details');
            d.className = 'thought';
            d.innerHTML = '<summary>Thinking</summary>';
            turn.thoughtEl = document.createElement('div');
            turn.thoughtEl.className = 'thought-body';
            d.appendChild(turn.thoughtEl);
            turn.bodyEl.appendChild(d);
        }
        turn.thoughtEl.textContent += delta;
        if (isNudge) {
            // Close the nudge thought so later model thoughts stream into a fresh block.
            turn.thoughtEl = null;
        }
        ensureTyping(turn);
    }

    function addToolCall(name, inp, turn) {
        turn.textEl = null;
        turn.thoughtEl = null;
        const card = document.createElement('details');
        card.className = 'tool-card running';
        card.innerHTML =
            '<summary class="tool-head"><span class="tool-spinner"></span>'
            + `<span class="tool-name">${escapeHtml(name)}</span>`
            + `<span class="tool-preview">${escapeHtml(inp || '')}</span></summary>`
            + `<div class="tool-input">${escapeHtml(inp || '')}</div>`
            + '<pre class="tool-output"></pre>';
        turn.bodyEl.appendChild(card);
        turn.toolCard = card;
        ensureTyping(turn);
    }

    function addToolResult(output, turn) {
        if (turn.toolCard) {
            turn.toolCard.classList.remove('running');
            turn.toolCard.classList.add('done');
            turn.toolCard.querySelector('.tool-output').textContent = output || '';
            turn.toolCard = null;
            turn.textEl = null;
        }
        ensureTyping(turn);
    }

    function handleEvent(ev, turn, log) {
        if (turn.finished) return turn;
        switch (ev.type) {
            case 'thought':
                appendThought(ev.text, turn, ev);
                break;
            case 'text':
                appendText(ev.text, turn, log);
                break;
            case 'tool_call':
                addToolCall(ev.name, ev.input, turn);
                break;
            case 'tool_result':
                addToolResult(ev.output, turn);
                break;
            case 'done':
                clearTyping(turn);
                if (!turn.bodyEl.querySelector('.msg-text') && ev.text) {
                    appendText(ev.text, turn, log);
                    clearTyping(turn);
                }
                if (!turn.bodyEl.textContent.trim()) turn.div.remove();
                turn.finished = true;
                break;
            case 'stopped':
                clearTyping(turn);
                appendText(ev.text || '(Stopped)', turn, log);
                clearTyping(turn);
                turn.finished = true;
                break;
            case 'error':
                clearTyping(turn);
                appendText(`[Error] ${ev.error}`, turn, log);
                clearTyping(turn);
                turn.finished = true;
                break;
        }
        return turn;
    }

    function appendUser(log, text, images) {
        const userDiv = ChatRender.appendUserMessage(log, text || '(image attached)', RENDER_OPTS);
        if (images && images.length && userDiv) {
            const strip = document.createElement('div');
            strip.className = 'wfc-msg-images';
            for (const img of images) {
                const thumb = document.createElement('img');
                thumb.src = img.dataUrl;
                strip.appendChild(thumb);
            }
            userDiv.appendChild(strip);
        }
        return userDiv;
    }

    async function consumeSse(res, turn, log) {
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buf = '';
        while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            buf += decoder.decode(value, { stream: true });
            let idx;
            while ((idx = buf.indexOf('\n\n')) !== -1) {
                const frame = buf.slice(0, idx);
                buf = buf.slice(idx + 2);
                if (!frame.startsWith('data:')) continue;
                const raw = frame.slice(5).trim();
                if (!raw) continue;
                const stick = atBottom(log);
                turn = handleEvent(JSON.parse(raw), turn, log);
                if (stick) toBottom(log);
            }
        }
    }

    async function streamChat({ log, message, images, context, onDone, onError }) {
        appendUser(log, message, images);
        const turn = startAssistant(log);
        toBottom(log);
        const payload = { message: message || '' };
        if (images && images.length) {
            payload.images = images.map((img) => ({ data: img.data }));
        }
        if (context && context.view) {
            payload.context = context;
        }
        try {
            const res = await fetch('/api/chat/stream', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Request-Id': (crypto.randomUUID
                        ? crypto.randomUUID()
                        : `${Date.now()}-${Math.random()}`),
                },
                body: JSON.stringify(payload),
            });
            if (!res.ok || !res.body) {
                const err = await res.json().catch(() => ({ error: res.statusText }));
                appendText(`[Error] ${err.error || res.statusText}`, turn, log);
                turn.finished = true;
                if (onError) onError(err.error || res.statusText);
                else if (onDone) onDone(turn);
                return turn;
            }
            await consumeSse(res, turn, log);
            if (!turn.finished) turn.finished = true;
            if (onDone) onDone(turn);
        } catch (err) {
            appendText(`[Error] ${err.message}`, turn, log);
            turn.finished = true;
            if (onError) onError(err.message);
            else if (onDone) onDone(turn);
        }
        return turn;
    }

    /** Reattach to an in-flight turn after hydrate (no new user message). */
    async function attachStream({ log, channel = 'main', onDone, onError }) {
        const turn = startAssistant(log);
        toBottom(log);
        const streamChannel = channel || 'main';
        try {
            let res = null;
            // Brief retry — scheduler opens the hub just as the tick starts.
            for (let attempt = 0; attempt < 12; attempt++) {
                res = await fetch(
                    `/api/chat/stream/attach?channel=${encodeURIComponent(streamChannel)}`,
                );
                if (res.status !== 404) break;
                await new Promise((r) => setTimeout(r, 250));
            }
            if (res.status === 404) {
                turn.div.remove();
                if (onDone) onDone(null);
                return null;
            }
            if (!res.ok || !res.body) {
                const err = await res.json().catch(() => ({ error: res.statusText }));
                turn.div.remove();
                if (onError) onError(err.error || res.statusText);
                else if (onDone) onDone(null);
                return null;
            }
            await consumeSse(res, turn, log);
            if (!turn.finished) turn.finished = true;
            if (!turn.bodyEl.textContent.trim()) turn.div.remove();
            if (onDone) onDone(turn);
        } catch (err) {
            appendText(`[Error] ${err.message}`, turn, log);
            turn.finished = true;
            if (onError) onError(err.message);
            else if (onDone) onDone(turn);
        }
        return turn;
    }

    async function stopChat(channel = 'main') {
        const streamChannel = channel || 'main';
        const res = await fetch('/api/chat/stop', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ channel: streamChannel }),
        });
        return res.json().catch(() => ({}));
    }

    async function selectRun(runId) {
        const res = await fetch('/api/chat/select', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ run_id: runId }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || 'Failed to select conversation');
        return data;
    }

    async function clearChat() {
        const res = await fetch('/api/clear', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ channel: 'main' }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || 'Failed to clear conversation');
        return data;
    }

    async function fetchHistory() {
        const res = await fetch('/api/history?channel=main');
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || 'Failed to load history');
        return data;
    }

    /** Populate one or more <select> elements from /api/model and keep them in sync. */
    async function loadModelSelects(selects) {
        const nodes = (Array.isArray(selects) ? selects : [selects]).filter(Boolean);
        if (!nodes.length) return null;
        try {
            const res = await fetch('/api/model?channel=main');
            const data = await res.json();
            if (!res.ok) throw new Error(data.error || 'Failed to load model');
            for (const el of nodes) {
                el.innerHTML = '';
                for (const m of data.options || []) {
                    const opt = document.createElement('option');
                    opt.value = m;
                    opt.textContent = m;
                    if (m === data.model) opt.selected = true;
                    el.appendChild(opt);
                }
                el.dataset.current = data.model || '';
                el.hidden = !(data.options || []).length;
                if (!data.persisted) {
                    el.title = 'Model resets on restart — set MONGO_URI / MONGO_DB to persist';
                } else {
                    el.title = 'Agent model';
                }
            }
            return data;
        } catch (e) {
            for (const el of nodes) el.hidden = true;
            return null;
        }
    }

    /** Grow a composer textarea upward until ~30% of its chat host height, then scroll. */
    function growComposerInput(el) {
        if (!el) return;
        const host = el.closest('.runs-chat, .wfc-panel') || document.documentElement;
        const total = host.clientHeight || window.innerHeight || 0;
        const max = Math.max(72, Math.floor(total * 0.3));
        // Temporarily clear constraints so scrollHeight reflects full content.
        el.style.maxHeight = 'none';
        el.style.height = 'auto';
        const needed = el.scrollHeight;
        const next = Math.min(needed, max);
        el.style.maxHeight = max + 'px';
        el.style.height = next + 'px';
        el.style.overflowY = needed > max ? 'auto' : 'hidden';
    }

    function bindAutoGrowInputs(inputs) {
        const nodes = (Array.isArray(inputs) ? inputs : [inputs]).filter(Boolean);
        function growAll() {
            for (const el of nodes) growComposerInput(el);
        }
        for (const el of nodes) {
            el.addEventListener('input', () => growComposerInput(el));
            // Catch Shift+Enter before the new line is painted.
            el.addEventListener('keydown', (event) => {
                if (event.key === 'Enter' && event.shiftKey) {
                    requestAnimationFrame(() => growComposerInput(el));
                }
            });
            growComposerInput(el);
        }
        window.addEventListener('resize', growAll);
        return growAll;
    }

    function bindModelSelects(selects) {
        const nodes = (Array.isArray(selects) ? selects : [selects]).filter(Boolean);
        async function changeModel(source) {
            const model = source.value;
            const prev = source.dataset.current || model;
            try {
                const res = await fetch('/api/model', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ model, channel: 'main' }),
                });
                const data = await res.json();
                if (!res.ok) throw new Error(data.error || 'Failed to set model');
                for (const el of nodes) {
                    el.value = data.model;
                    el.dataset.current = data.model;
                }
                const dash = document.getElementById('model-select');
                if (dash) dash.value = data.model;
            } catch (err) {
                source.value = prev;
                if (window.towerToast) window.towerToast(err.message || 'Failed to set model', { type: 'error' }); else alert(err.message || 'Failed to set model');
            }
        }
        for (const el of nodes) {
            el.addEventListener('change', () => changeModel(el));
        }
    }

    return {
        RENDER_OPTS,
        hydrate,
        toBottom,
        streamChat,
        attachStream,
        stopChat,
        selectRun,
        clearChat,
        fetchHistory,
        loadModelSelects,
        bindModelSelects,
        growComposerInput,
        bindAutoGrowInputs,
        appendUser,
        startAssistant,
        handleEvent,
        appendText,
    };
})();
