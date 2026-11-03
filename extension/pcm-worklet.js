// Turns the captured tab's float audio into 16-bit stereo PCM in 20 ms blocks (960 frames at 48 kHz) and posts each block.
class PcmBlocks extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buf = new Int16Array(960 * 2);
    this.n = 0;
  }
  process(inputs) {
    const input = inputs[0];
    if (!input || !input.length) return true;
    const left = input[0], right = input[1] || input[0];
    for (let i = 0; i < left.length; i++) {
      const l = Math.max(-1, Math.min(1, left[i])), r = Math.max(-1, Math.min(1, right[i]));
      this.buf[this.n++] = l < 0 ? l * 32768 : l * 32767;
      this.buf[this.n++] = r < 0 ? r * 32768 : r * 32767;
      if (this.n === this.buf.length) {
        const out = this.buf.slice();
        this.port.postMessage(out.buffer, [out.buffer]);
        this.n = 0;
      }
    }
    return true;
  }
}
registerProcessor("pcm-blocks", PcmBlocks);
