// Spike: start/stop capture of a tab. Triggered by action click (real use) or by evaluating startCapture() in this worker (automation).
async function ensureOffscreen() {
  if (await chrome.offscreen.hasDocument()) return;
  await chrome.offscreen.createDocument({url: 'offscreen.html', reasons: ['USER_MEDIA'], justification: 'tab audio capture'});
}
async function startCapture(tabId, sink) {
  const streamId = await chrome.tabCapture.getMediaStreamId({targetTabId: tabId});
  await ensureOffscreen();
  chrome.runtime.sendMessage({type: 'start', streamId, sink});
  return streamId;
}
async function stopCapture() { chrome.runtime.sendMessage({type: 'stop'}); }
chrome.action.onClicked.addListener(tab => startCapture(tab.id, 'ws://127.0.0.1:8799/ingest'));
self.startCapture = startCapture; self.stopCapture = stopCapture;
