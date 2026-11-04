# "Also play here" plays in step with the speaker

**Date**: 2026-11-04  
**Status**: In Progress  
**Source**: the user's question in chat (2026-11-04): can the YouTube delays be synced up? Agreed: delay the Mac's own copy of a streamed tab ("Also play here", FR20) by about the speaker's delay so the two sound together; the video picture can't be held back, and the speaker's ~3–4 s buffer can't be shortened. "Exact syncing is not necessary so nudging isn't necessary": one fixed delay, no adjust control. Spec FR20, NFR2 (first figure: roughly 3.6–4.7 s tab to speaker on Office), epic 01-07 Story 3.  
**Effort**: medium  
**Audit**: pass after fixes (2026-11-04), 2 rounds: round 1 (1 Warning: `stream_driver.py` read the local branch 1.5–3 s in, which the 4 s delay makes read like off, and epic 01-07 Story 3's FR20 Evidence described the undelayed behaviour; Suggestions taken: evidence quoting the replaced build, the log's labels, FR20's "off needs no code"; not taken: smoothing the gain step) and round 2 (pass)  
**Signal**: harness-reads-before-the-effect — project — a harness reading taken a fixed time after an action silently changes meaning when the code adds latency (stream_driver.py read Also play here at 1.5 s; derive waits from the code's constant)  

**Description**: In `extension/offscreen.js` the captured tab audio reaches this Mac's speakers through a `DelayNode` set to a fixed 4 s (the middle of NFR2's first figure), always fed from the capture. "Also play here" switches the delayed output on and off, so ticking it mid-stream plays at once what the speaker is playing now, and unticking it is silent at once. The stream to the service is not delayed. Off by default, as before.

**Acceptance Criteria**:

- With "Also play here" ticked before Stream tab on the 440 Hz test page, the level on the local branch (`self.localLevel()`) stays near silent (RMS under 0.01) for at least the first 3 s after the stream answers and has the tone (RMS over 0.05) by 5.5 s; the stream to the service has the tone from the start. Control: with the delay set to 0 the local branch has the tone within 1 s. `[browser]`
  - Evidence: browser-observed with `tests/extension_harness/delay_driver.py` (log: docs/evidence/quick-05-also-play-here-delay.log): branded Chrome 156, the unmodified `extension/` loaded by its pinned ID, the fake-rooms service from `.venv` on :8737 (the installed service stopped for the run, with the user's leave), the popup's real Stream tab and Also play here controls on the 440 Hz test page, `self.localLevel()` read every 0.25 s over CDP. Ticked before Stream tab (the shipped, gain-switched build): max RMS 0 in the first 3 s after the stream answered, first over 0.05 at 4.05 s; the stream the service served had a 439.8 Hz tone in its first 3 s. Control (`--delay 0`, a copy of the extension with `LOCAL_DELAY_S = 0`): first over 0.05 at 0.0 s, max 0.21 in the first 3 s, so the check fails without the delay — red: n/a — browser-observed
- Ticked while streaming (after the delay has filled), the local branch has the tone within 1 s; unticked, it is near silent within 1 s; with "Also play here" off throughout, it is near silent throughout (the existing FR20 behaviour). `[browser]`
  - Evidence: browser-observed with `tests/extension_harness/delay_driver.py` (log: docs/evidence/quick-05-also-play-here-delay.log): branded Chrome 156, the unmodified `extension/` loaded by its pinned ID, the fake-rooms service from `.venv` on :8737 (the installed service stopped for the run, with the user's leave), the popup's real Stream tab and Also play here controls on the 440 Hz test page, `self.localLevel()` read every 0.25 s over CDP. Off for the first 5.5 s: max RMS 0; ticked: over 0.05 at 0.25 s; unticked: under 0.01 at 0.25 s; the stream still answering. Found on the way, the first build failed this: with the delay's output disconnected while unticked, Chrome did not run the delay, so ticking mid-stream stayed silent for the 2 s watched (every reading 0); the delay now stays connected and a gain switches it — red: n/a — browser-observed
- README's Chrome extension section and spec FR20 say the local copy plays about 4 s late, in step with the speaker. `[manual]`
  - Evidence: README.md's Chrome extension section now reads "*Also play here* keeps the tab audible on this Mac while it streams, held back about 4 s so it sounds together with the speaker"; spec FR20 says the local copy goes through a fixed delay of about 4 s and the stream to the service is not delayed, with a header Amended line — red: n/a — manual
- On Office with "Also play here" on, a YouTube tab heard from the Mac and from the Office sounds roughly together (no echo of several seconds). `[user]`
  - Waiting on user: on a Mac with the reloaded extension (chrome://extensions → the reload arrow on Sonos at home), stream a YouTube music video to Office with Also play here ticked, and say whether the Mac and the Office sound roughly together.

### Delay the local branch
**Task**: 5.1  
**Description**: `DelayNode` (max 5 s, set to `LOCAL_DELAY_S = 4`) between the capture and the local gain in `begin()`; the delay stays connected and `setLocal()` switches the local gain between 0 and 1 (a disconnected delay is not run by Chrome, so it would never fill). A harness driver (`tests/extension_harness/delay_driver.py`) that runs the service from `.venv` (never `uv run`), refuses to start when port 8737 is taken, and times the local level.  
**Status**: Complete

### Limits

- An instant gain step on tick or untick can click, as the old connect and disconnect did (audit round 1 Suggestion, not taken: re-observing it would mean stopping the user's service again).
- Epic 01-07 Story 3's FR20 Evidence describes the undelayed behaviour; a note beside it points here, and `stream_driver.py` now waits `LOCAL_DELAY_S` + 1.5 s before reading the local branch (found by audit round 1; not re-run, since it needs port 8737).
