# Extension capture harness (spike 01-01 Story 3)

Proves Chrome tab capture can be started and measured with no human click. Not collected by pytest.

    uv run --with websockets python tests/extension_harness/sink.py &      # PCM sink on ws://127.0.0.1:8799
    uv run --with websockets python tests/extension_harness/driver.py 60   # capture a 440 Hz tab for 60 s

`driver.py` launches branded Chrome with a throwaway profile and these flags: `--remote-debugging-port`,
`--enable-unsafe-extension-debugging`, `--allowlisted-extension-id=<id>`, `--auto-accept-this-tab-capture`,
`--autoplay-policy=no-user-gesture-required`. It loads `ext/` with the CDP command `Extensions.loadUnpacked`, because
Chrome 156 ignores `--load-extension`. The `key` in `ext/manifest.json` pins the extension ID to `extid.txt`. The driver
then evaluates `startCapture(tabId, sink)` in the extension's service worker. The allowlist flag is what lets
`chrome.tabCapture.getMediaStreamId` succeed without the activeTab invocation a toolbar click gives; without it the call
fails with "Extension has not been invoked for the current page".
