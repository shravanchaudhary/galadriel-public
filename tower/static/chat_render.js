/** Shared Tower chat history + block rendering (Mirror + overlay widget). */
window.ChatRender = (function () {
    function escapeHtml(text) {
        const d = document.createElement('div');
        d.textContent = text;
        return d.innerHTML;
    }

    function createThoughtBlock(text, openDefault) {
        const d = document.createElement('details');
        d.className = 'thought';
        if (openDefault) d.open = true;
        d.innerHTML = '<summary>Thought process</summary>';
        const body = document.createElement('div');
        body.className = 'thought-body';
        body.textContent = text;
        d.appendChild(body);
        return d;
    }

    function createToolCard(name, input, output) {
        const card = document.createElement('details');
        card.className = 'tool-card done';
        card.innerHTML =
            '<summary class="tool-head"><span class="tool-spinner"></span>'
            + '<span class="tool-name">' + escapeHtml(name || '') + '</span>'
            + '<span class="tool-preview">' + escapeHtml(input || '') + '</span></summary>'
            + '<div class="tool-input">' + escapeHtml(input || '') + '</div>'
            + '<pre class="tool-output"></pre>';
        card.querySelector('.tool-output').textContent = output || '';
        return card;
    }

    function createTextBlock(text) {
        const el = document.createElement('div');
        el.className = 'msg-text';
        el.textContent = text;
        return el;
    }

    function appendUserMessage(log, text, opts) {
        const div = document.createElement('div');
        div.className = opts.userClass || 'msg msg-user';
        const textClass = opts.userTextClass || 'msg-text';
        let html = '';
        if (opts.userLabel) html += '<strong>' + escapeHtml(opts.userLabel) + '</strong>';
        html += '<div class="' + textClass + '">' + escapeHtml(text) + '</div>';
        div.innerHTML = html;
        log.appendChild(div);
        return div;
    }

    function appendAssistantTurn(log, blocks, opts) {
        const div = document.createElement('div');
        div.className = opts.assistantClass || 'msg msg-assistant';
        if (opts.assistantLabel) {
            const label = document.createElement('strong');
            label.textContent = opts.assistantLabel;
            div.appendChild(label);
        }
        const body = document.createElement('div');
        body.className = 'msg-body';
        for (const block of blocks || []) {
            if (block.type === 'thought') {
                body.appendChild(createThoughtBlock(block.text, false));
            } else if (block.type === 'tool_call') {
                body.appendChild(createToolCard(block.name, block.input, block.output));
            } else if (block.type === 'text') {
                body.appendChild(createTextBlock(block.text));
            }
        }
        div.appendChild(body);
        log.appendChild(div);
        return div;
    }

    function appendHistoryEntry(log, msg, opts) {
        if (msg.role === 'user') {
            return appendUserMessage(log, msg.text, opts);
        }
        if (msg.role === 'assistant' && msg.blocks) {
            return appendAssistantTurn(log, msg.blocks, opts);
        }
        if (msg.text) {
            const role = msg.role === 'user' ? 'user' : 'assistant';
            const fallbackOpts = Object.assign({}, opts);
            if (role === 'assistant') {
                return appendAssistantTurn(log, [{ type: 'text', text: msg.text }], fallbackOpts);
            }
            return appendUserMessage(log, msg.text, fallbackOpts);
        }
        return null;
    }

    function hydrateHistory(log, entries, opts) {
        for (const msg of entries) {
            appendHistoryEntry(log, msg, opts);
        }
    }

    return {
        escapeHtml,
        createThoughtBlock,
        createToolCard,
        createTextBlock,
        appendUserMessage,
        appendAssistantTurn,
        appendHistoryEntry,
        hydrateHistory,
    };
})();
