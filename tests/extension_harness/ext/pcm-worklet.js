class PCM extends AudioWorkletProcessor {
  constructor() { super(); this.buf = new Int16Array(960 * 2); this.n = 0; }   // 20 ms @ 48k stereo
  process(inputs) {
    const inp = inputs[0]; if (!inp || !inp.length) return true;
    const l = inp[0], r = inp[1] || inp[0];
    for (let i = 0; i < l.length; i++) {
      const cl = Math.max(-1, Math.min(1, l[i])), cr = Math.max(-1, Math.min(1, r[i]));
      this.buf[this.n++] = cl < 0 ? cl * 32768 : cl * 32767;
      this.buf[this.n++] = cr < 0 ? cr * 32768 : cr * 32767;
      if (this.n === this.buf.length) { const out = this.buf.slice(); this.port.postMessage(out.buffer, [out.buffer]); this.n = 0; }
    }
    return true;
  }
}
registerProcessor('pcm', PCM);
