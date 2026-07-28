import {
    StartStreamTranscriptionCommand,
    TranscribeStreamingClient,
} from '@aws-sdk/client-transcribe-streaming';

const SAMPLE_RATE = 16000;
const MIC_ICON = `
    <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
        <path fill="currentColor" d="M12 14a3 3 0 0 0 3-3V5a3 3 0 1 0-6 0v6a3 3 0 0 0 3 3Zm-1-9a1 1 0 1 1 2 0v6a1 1 0 1 1-2 0V5Zm7 6a6 6 0 0 1-5 5.92V20h3v2H8v-2h3v-3.08A6 6 0 0 1 6 11h2a4 4 0 0 0 8 0h2Z"/>
    </svg>`;

class AudioQueue {
    constructor() {
        this.values = [];
        this.waiters = [];
        this.closed = false;
    }

    push(value) {
        if (this.closed) return;
        const waiter = this.waiters.shift();
        if (waiter) waiter({ value, done: false });
        else this.values.push(value);
    }

    close() {
        if (this.closed) return;
        this.closed = true;
        while (this.waiters.length) this.waiters.shift()({ value: undefined, done: true });
    }

    [Symbol.asyncIterator]() {
        return this;
    }

    next() {
        if (this.values.length) return Promise.resolve({ value: this.values.shift(), done: false });
        if (this.closed) return Promise.resolve({ value: undefined, done: true });
        return new Promise((resolve) => this.waiters.push(resolve));
    }
}

function joinedTranscript(results) {
    return Array.from(results.values())
        .map((result) => result.text.trim())
        .filter(Boolean)
        .join(' ');
}

class DictationController {
    constructor(input, button, options = {}) {
        this.input = input;
        this.button = button;
        this.languageCode = options.languageCode || document.documentElement.lang || 'en-US';
        if (!this.languageCode.includes('-')) this.languageCode = 'en-US';
        this.state = 'idle';
        this.results = new Map();
        this.currentTranscript = '';
        this.ignoredTranscript = '';
        this.rangeStart = 0;
        this.rangeEnd = 0;
        this.lastValue = input.value;
        this.applying = false;
        this.desiredListening = false;
        this.onManualInput = this.onManualInput.bind(this);
        this.button.innerHTML = MIC_ICON;
        this.button.addEventListener('click', () => this.toggle());
        this.setState('idle');
    }

    supported() {
        return !!(
            window.isSecureContext
            && navigator.mediaDevices?.getUserMedia
            && window.AudioContext
            && window.AudioWorkletNode
        );
    }

    setState(state, detail = '') {
        this.state = state;
        this.button.dataset.voiceState = state;
        this.button.setAttribute('aria-pressed', state === 'listening' ? 'true' : 'false');
        const labels = {
            idle: 'Start voice dictation',
            connecting: 'Connecting voice dictation…',
            listening: 'Stop voice dictation',
            stopping: 'Stopping voice dictation…',
            error: detail || 'Voice dictation failed',
            unsupported: detail || 'Voice dictation is unavailable in this browser',
        };
        this.button.title = labels[state];
        this.button.setAttribute('aria-label', labels[state]);
        this.button.disabled = state === 'stopping' || state === 'unsupported';
        const status = this.button.parentElement?.querySelector('.voice-dictation-status');
        if (status) status.textContent = state === 'error' || state === 'unsupported' ? labels[state] : '';
    }

    async toggle() {
        if (this.state === 'listening' || this.state === 'connecting') {
            await this.stop();
        } else if (this.state !== 'stopping') {
            await this.start();
        }
    }

    async start() {
        if (!this.supported()) {
            this.setState('unsupported', window.isSecureContext
                ? 'Voice dictation is unavailable in this browser'
                : 'Voice dictation requires HTTPS');
            return;
        }

        this.desiredListening = true;
        this.results.clear();
        this.currentTranscript = '';
        this.ignoredTranscript = '';
        this.rangeStart = this.input.selectionStart ?? this.input.value.length;
        this.rangeEnd = this.rangeStart;
        this.lastValue = this.input.value;
        this.input.addEventListener('input', this.onManualInput);
        this.abortController = new AbortController();
        this.setState('connecting');

        try {
            const [credentialsResponse, stream] = await Promise.all([
                fetch('/api/transcribe/credentials', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: '{}',
                }),
                navigator.mediaDevices.getUserMedia({
                    audio: {
                        channelCount: 1,
                        echoCancellation: true,
                        noiseSuppression: true,
                        autoGainControl: true,
                    },
                }),
            ]);
            if (!credentialsResponse.ok) {
                const body = await credentialsResponse.json().catch(() => ({}));
                throw new Error(body.error || 'Could not connect voice dictation');
            }
            const credentials = await credentialsResponse.json();
            if (!this.desiredListening) {
                stream.getTracks().forEach((track) => track.stop());
                return;
            }

            this.stream = stream;
            this.queue = new AudioQueue();
            this.audioContext = new AudioContext({ sampleRate: SAMPLE_RATE });
            await this.audioContext.audioWorklet.addModule('/static/voice_audio_worklet.js');
            if (!this.desiredListening) return;
            await this.audioContext.resume();
            this.sourceNode = this.audioContext.createMediaStreamSource(stream);
            this.workletNode = new AudioWorkletNode(this.audioContext, 'voice-pcm-processor');
            this.silentGain = this.audioContext.createGain();
            this.silentGain.gain.value = 0;
            this.workletNode.port.onmessage = (event) => {
                if (this.queue && event.data?.byteLength) {
                    this.queue.push({
                        AudioEvent: { AudioChunk: new Uint8Array(event.data) },
                    });
                }
            };
            this.sourceNode.connect(this.workletNode);
            this.workletNode.connect(this.silentGain);
            this.silentGain.connect(this.audioContext.destination);

            this.consumePromise = this.consume(credentials).catch((error) => {
                if (this.desiredListening && error?.name !== 'AbortError') this.fail(error);
            });
            this.setState('listening');
        } catch (error) {
            if (this.desiredListening) this.fail(error);
            else await this.cleanupAudio();
        }
    }

    async consume(credentials) {
        const client = new TranscribeStreamingClient({
            region: credentials.region,
            credentials: {
                accessKeyId: credentials.accessKeyId,
                secretAccessKey: credentials.secretAccessKey,
                sessionToken: credentials.sessionToken,
                expiration: credentials.expiration
                    ? new Date(credentials.expiration)
                    : undefined,
            },
        });
        this.transcribeClient = client;
        const response = await client.send(new StartStreamTranscriptionCommand({
            LanguageCode: this.languageCode,
            MediaEncoding: 'pcm',
            MediaSampleRateHertz: SAMPLE_RATE,
            EnablePartialResultsStabilization: true,
            PartialResultsStability: 'medium',
            AudioStream: this.queue,
        }), { abortSignal: this.abortController.signal });

        for await (const event of response.TranscriptResultStream || []) {
            for (const result of event.TranscriptEvent?.Transcript?.Results || []) {
                const text = result.Alternatives?.[0]?.Transcript || '';
                if (!result.ResultId) continue;
                this.results.set(result.ResultId, { text, partial: !!result.IsPartial });
            }
            this.currentTranscript = joinedTranscript(this.results);
            this.renderTranscript();
        }
    }

    renderTranscript() {
        let text = this.currentTranscript;
        if (this.ignoredTranscript) {
            text = text.startsWith(this.ignoredTranscript)
                ? text.slice(this.ignoredTranscript.length).trimStart()
                : '';
        }
        const left = this.input.value.slice(0, this.rangeStart);
        const right = this.input.value.slice(this.rangeEnd);
        const leading = text && left && !/\s$/.test(left) && !/^[,.;:!?]/.test(text) ? ' ' : '';
        const trailing = text && right && !/^\s|^[,.;:!?]/.test(right) ? ' ' : '';
        const replacement = leading + text + trailing;
        this.applying = true;
        this.input.setRangeText(replacement, this.rangeStart, this.rangeEnd, 'end');
        this.rangeEnd = this.rangeStart + replacement.length;
        this.lastValue = this.input.value;
        this.input.dispatchEvent(new Event('input', { bubbles: true }));
        this.lastValue = this.input.value;
        this.applying = false;
    }

    onManualInput() {
        if (this.applying) return;
        const oldValue = this.lastValue;
        const newValue = this.input.value;
        let prefix = 0;
        while (prefix < oldValue.length && prefix < newValue.length
            && oldValue[prefix] === newValue[prefix]) prefix += 1;
        let suffix = 0;
        while (suffix < oldValue.length - prefix && suffix < newValue.length - prefix
            && oldValue[oldValue.length - 1 - suffix] === newValue[newValue.length - 1 - suffix]) {
            suffix += 1;
        }
        const oldChangeEnd = oldValue.length - suffix;
        const newChangeEnd = newValue.length - suffix;
        if (oldChangeEnd <= this.rangeStart) {
            const delta = newValue.length - oldValue.length;
            this.rangeStart += delta;
            this.rangeEnd += delta;
        } else if (prefix < this.rangeEnd) {
            this.ignoredTranscript = this.currentTranscript;
            this.rangeStart = newChangeEnd;
            this.rangeEnd = newChangeEnd;
        }
        this.lastValue = newValue;
    }

    async stop() {
        this.desiredListening = false;
        if (this.state !== 'error') this.setState('stopping');
        this.queue?.close();
        if (this.consumePromise) {
            await Promise.race([
                this.consumePromise,
                new Promise((resolve) => setTimeout(resolve, 1200)),
            ]);
        }
        this.abortController?.abort();
        await this.cleanupAudio();
        this.input.removeEventListener('input', this.onManualInput);
        this.input.focus();
        this.setState('idle');
    }

    async cleanupAudio() {
        this.sourceNode?.disconnect();
        this.workletNode?.disconnect();
        this.silentGain?.disconnect();
        this.stream?.getTracks().forEach((track) => track.stop());
        if (this.audioContext && this.audioContext.state !== 'closed') {
            await this.audioContext.close().catch(() => {});
        }
        this.transcribeClient?.destroy();
        this.sourceNode = null;
        this.workletNode = null;
        this.silentGain = null;
        this.stream = null;
        this.audioContext = null;
        this.queue = null;
        this.consumePromise = null;
        this.transcribeClient = null;
    }

    async fail(error) {
        this.desiredListening = false;
        this.abortController?.abort();
        await this.cleanupAudio();
        this.input.removeEventListener('input', this.onManualInput);
        let message = error?.message || 'Voice dictation failed';
        if (error?.name === 'NotAllowedError') message = 'Microphone permission was denied';
        else if (error?.name === 'NotFoundError') message = 'No microphone was found';
        this.setState('error', message);
    }
}

window.VoiceDictation = {
    bind({ input, button, languageCode }) {
        if (!input || !button) return null;
        let status = button.parentElement?.querySelector('.voice-dictation-status');
        if (!status) {
            status = document.createElement('span');
            status.className = 'voice-dictation-status sr-only';
            status.setAttribute('aria-live', 'polite');
            button.insertAdjacentElement('afterend', status);
        }
        const controller = new DictationController(input, button, { languageCode });
        if (!controller.supported()) controller.setState('unsupported');
        return controller;
    },
};
