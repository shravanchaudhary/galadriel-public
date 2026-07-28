class VoicePcmProcessor extends AudioWorkletProcessor {
    constructor() {
        super();
        this.targetRate = 16000;
        this.phase = 0;
        this.sum = 0;
        this.count = 0;
        this.samples = [];
        this.chunkSamples = 1600;
    }

    process(inputs, outputs) {
        const input = inputs[0]?.[0];
        const output = outputs[0]?.[0];
        if (output) output.fill(0);
        if (!input) return true;

        for (let index = 0; index < input.length; index += 1) {
            this.sum += input[index];
            this.count += 1;
            this.phase += this.targetRate;
            if (this.phase >= sampleRate) {
                const sample = Math.max(-1, Math.min(1, this.sum / this.count));
                this.samples.push(sample < 0 ? sample * 0x8000 : sample * 0x7fff);
                this.phase -= sampleRate;
                this.sum = 0;
                this.count = 0;
            }
        }

        if (this.samples.length >= this.chunkSamples) {
            const pcm = new Int16Array(this.samples.splice(0, this.chunkSamples));
            this.port.postMessage(pcm.buffer, [pcm.buffer]);
        }
        return true;
    }
}

registerProcessor('voice-pcm-processor', VoicePcmProcessor);
