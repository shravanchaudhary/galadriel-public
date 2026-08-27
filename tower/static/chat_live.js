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

    /** A failed call, in plain text and red — not the JSON payload. */
    function appendError(message, turn, log) {
        if (!turn) turn = startAssistant(log);
        const el = document.createElement('div');
        el.className = 'msg-text msg-error';
        el.textContent = message;
        turn.bodyEl.appendChild(el);
        turn.textEl = null;
        return turn;
    }

    function appendThought(delta, turn, opts) {
        const isRecallFire = opts && opts.kind === 'recall_fire';
        if (isRecallFire) {
            // Recall fires are learned-behavior notes, not model thoughts —
            // self-contained collapsed block; later thoughts stream fresh.
            turn.bodyEl.appendChild(ChatRender.createLearnedBlock(delta));
            turn.thoughtEl = null;
            ensureTyping(turn);
            return;
        }
        if (!turn.thoughtEl) {
            const d = document.createElement('details');
            d.className = 'thought';
            d.innerHTML = '<summary>Thinking</summary>';
            turn.thoughtEl = document.createElement('div');
            turn.thoughtEl.className = 'thought-body';
            d.appendChild(turn.thoughtEl);
            turn.bodyEl.appendChild(d);
        }
        turn.thoughtEl.textContent += delta;
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
                appendError(errorText(ev), turn, log);
                clearTyping(turn);
                turn.finished = true;
                turn.errored = true;
                break;
        }
        return turn;
    }

    /** A short, human sentence for the chat log — never the raw JSON payload. */
    const GENERIC_ERROR = "Something went wrong on our end. Please try again in a moment.";

    function errorText(ev) {
        const d = ev.detail;
        // Full model/provider/HTTP detail stays in devtools for whoever's
        // debugging; the person chatting just needs to know it failed.
        if (d) console.error('[chat error]', d);
        const message = (d && d.message) || ev.error;
        if (!message || typeof message !== 'string') return GENERIC_ERROR;
        // Provider messages are sometimes still a raw JSON blob despite the
        // backend's best effort to unwrap them — never show that to the user.
        const trimmed = message.trim();
        if (trimmed.startsWith('{') || trimmed.startsWith('[')) return GENERIC_ERROR;
        return trimmed;
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
                appendError(errorText(err), turn, log);
                turn.finished = true;
                turn.errored = true;
                if (onError) onError(err.error || res.statusText, turn);
                else if (onDone) onDone(turn);
                return turn;
            }
            await consumeSse(res, turn, log);
            if (!turn.finished) turn.finished = true;
            if (onDone) onDone(turn);
        } catch (err) {
            appendError(errorText({ error: err.message }), turn, log);
            turn.finished = true;
            turn.errored = true;
            if (onError) onError(err.message, turn);
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
                const message = err.error || res.statusText;
                // Previously the turn was dropped silently here, so a failed
                // attach looked identical to a turn that never started.
                appendError(errorText(err), turn, log);
                turn.finished = true;
                turn.errored = true;
                if (onError) onError(message, turn);
                else if (onDone) onDone(turn);
                return turn;
            }
            await consumeSse(res, turn, log);
            if (!turn.finished) turn.finished = true;
            if (!turn.bodyEl.textContent.trim()) turn.div.remove();
            if (onDone) onDone(turn);
        } catch (err) {
            appendError(errorText({ error: err.message }), turn, log);
            turn.finished = true;
            turn.errored = true;
            if (onError) onError(err.message, turn);
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

    const CONTEXT_LABELS = { '300000': '300K', '1000000': '1M' };
    const EFFORT_LABELS = {
        off: 'Off',
        minimal: 'Minimal',
        low: 'Low',
        medium: 'Medium',
        high: 'High',
        dynamic: 'Dynamic',
    };

    function fillSelect(el, items, current, title) {
        if (!el) return;
        el.innerHTML = '';
        let selected = current == null ? '' : String(current);
        const availableValues = items
            .filter((item) => item && item.available !== false)
            .map((item) => String(item.value != null ? item.value : item));
        if (selected && availableValues.length && !availableValues.includes(selected)) {
            selected = availableValues[availableValues.length - 1] || '';
        }
        for (const item of items) {
            const value = String(item.value != null ? item.value : item);
            const label = item.label || CONTEXT_LABELS[value] || EFFORT_LABELS[value] || value;
            const available = item.available !== false;
            const opt = document.createElement('option');
            opt.value = value;
            opt.textContent = available ? label : label + ' (n/a)';
            opt.disabled = !available;
            if (!available) opt.className = 'is-unavailable';
            if (available && value === selected) opt.selected = true;
            el.appendChild(opt);
        }
        el.dataset.current = selected;
        el.hidden = availableValues.length === 0;
        if (title) el.title = title;
    }

    function effortItemsFor(data, model) {
        const rows = (data.effort_by_model && data.effort_by_model[model])
            || data.effort_catalog
            || data.effort_options
            || [];
        return rows.map((row) => {
            if (row && typeof row === 'object') {
                const value = row.value;
                return {
                    value,
                    label: row.label || EFFORT_LABELS[value] || value,
                    available: row.available !== false,
                };
            }
            return { value: row, label: EFFORT_LABELS[row] || row, available: true };
        });
    }

    // Vision gate. `model_labels[].vision` comes from model_catalog; a model
    // that cannot read images gets its composer attach buttons disabled rather
    // than a rejection after the fact.
    let imagesAllowed = true;
    let blindModel = '';

    function applyVisionGate(data) {
        const row = (data.model_labels || []).find((m) => m.value === data.model);
        imagesAllowed = !row || row.vision !== false;
        blindModel = imagesAllowed ? '' : data.model;
        for (const btn of document.querySelectorAll('[data-image-attach]')) {
            btn.disabled = !imagesAllowed;
            btn.title = imagesAllowed
                ? 'Attach image (or paste one)'
                : `${data.model} can't read images — switch models to attach one`;
        }
    }

    /** False when the selected model is text-only. */
    function canAttachImages() {
        return imagesAllowed;
    }

    /** Toast + reject an attachment the current model could not read. */
    function rejectImageAttach() {
        const msg = `${blindModel || 'This model'} can't read images — switch models to attach one`;
        if (window.towerToast) window.towerToast(msg, { type: 'error' }); else alert(msg);
    }

    function persistHint(data, ready, missing) {
        return data && data.persisted ? ready : missing;
    }

    function applyRuntime(data, groups) {
        if (!data || !groups) return data;
        const modelTitle = persistHint(
            data,
            'Agent model',
            'Model resets on restart — set MONGO_URI / MONGO_DB to persist',
        );
        // model_labels carries the display name plus intel score; options is the
        // bare id list, kept as a fallback for a server that predates it.
        const modelItems = (data.model_labels && data.model_labels.length)
            ? data.model_labels.map((m) => ({ value: m.value, label: m.label }))
            : (data.options || []).map((m) => ({ value: m, label: m }));
        for (const el of groups.models) {
            fillSelect(el, modelItems, data.model, modelTitle);
        }
        const contextTitle = persistHint(data, 'Context before compaction', 'Context resets on restart');
        for (const el of groups.contexts) {
            fillSelect(el, data.context_options || [], data.context, contextTitle);
        }
        const effortTitle = persistHint(data, 'Thinking effort', 'Effort resets on restart');
        const effortItems = effortItemsFor(data, data.model);
        for (const el of groups.efforts) {
            fillSelect(el, effortItems, data.effort, effortTitle);
        }
        applyVisionGate(data);
        return data;
    }

    function runtimeGroups(selects) {
        if (selects && !Array.isArray(selects) && (selects.models || selects.contexts || selects.efforts)) {
            return {
                models: (selects.models || []).filter(Boolean),
                contexts: (selects.contexts || []).filter(Boolean),
                efforts: (selects.efforts || []).filter(Boolean),
            };
        }
        return {
            models: (Array.isArray(selects) ? selects : [selects]).filter(Boolean),
            contexts: [],
            efforts: [],
        };
    }

    /** Populate composer selects from /api/model and keep them in sync. */
    async function loadModelSelects(selects) {
        const groups = runtimeGroups(selects);
        const nodes = [...groups.models, ...groups.contexts, ...groups.efforts];
        if (!nodes.length) return null;
        try {
            const res = await fetch('/api/model?channel=main');
            const data = await res.json();
            if (!res.ok) throw new Error(data.error || 'Failed to load model');
            return applyRuntime(data, groups);
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
        const groups = runtimeGroups(selects);
        async function postRuntime(url, body, label) {
            const res = await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.error || `Failed to set ${label}`);
            applyRuntime(data, groups);
            if (data.model) {
                const dash = document.getElementById('model-select');
                if (dash) dash.value = data.model;
            }
            return data;
        }
        async function changeModel(source) {
            const model = source.value;
            const prev = source.dataset.current || model;
            try {
                await postRuntime('/api/model', { model, channel: 'main' }, 'model');
            } catch (err) {
                source.value = prev;
                if (window.towerToast) window.towerToast(err.message || 'Failed to set model', { type: 'error' }); else alert(err.message || 'Failed to set model');
            }
        }
        async function changeContext(source) {
            const context = parseInt(source.value, 10);
            const prev = source.dataset.current || source.value;
            try {
                await postRuntime('/api/context', { context }, 'context');
            } catch (err) {
                source.value = prev;
                if (window.towerToast) window.towerToast(err.message || 'Failed to set context', { type: 'error' }); else alert(err.message || 'Failed to set context');
            }
        }
        async function changeEffort(source) {
            const effort = source.value;
            const prev = source.dataset.current || effort;
            try {
                await postRuntime('/api/effort', { effort }, 'effort');
            } catch (err) {
                source.value = prev;
                if (window.towerToast) window.towerToast(err.message || 'Failed to set effort', { type: 'error' }); else alert(err.message || 'Failed to set effort');
            }
        }
        for (const el of groups.models) {
            el.addEventListener('change', () => changeModel(el));
        }
        for (const el of groups.contexts) {
            el.addEventListener('change', () => changeContext(el));
        }
        for (const el of groups.efforts) {
            el.addEventListener('change', () => changeEffort(el));
        }
    }

    return {
        RENDER_OPTS,
        canAttachImages,
        rejectImageAttach,
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
