let ctx, ws, stream;
chrome.runtime.onMessage.addListener(async (m) => {
  if (m.type === 'start') {
    stream = await navigator.mediaDevices.getUserMedia({audio: {mandatory: {chromeMediaSource: 'tab', chromeMediaSourceId: m.streamId}}, video: false});
    ctx = new AudioContext({sampleRate: 48000});
    console.log('ctx rate', ctx.sampleRate);
    await ctx.audioWorklet.addModule('pcm-worklet.js');
    const src = ctx.createMediaStreamSource(stream);
    const node = new AudioWorkletNode(ctx, 'pcm', {numberOfInputs: 1, numberOfOutputs: 0, channelCount: 2, channelCountMode: 'explicit'});
    ws = new WebSocket(m.sink); ws.binaryType = 'arraybuffer';
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
    node.port.onmessage = e => { if (ws.readyState === 1) ws.send(e.data); };
    src.connect(node);
    src.connect(ctx.destination);          // keep the tab audible (capture otherwise mutes it)
    ws.onclose = () => console.log('ws closed');
  } else if (m.type === 'stop') {
    stream && stream.getTracks().forEach(t => t.stop()); ws && ws.close(); ctx && ctx.close();
  }
});
