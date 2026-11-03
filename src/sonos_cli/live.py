"""Live tab streams (FR15, FR16, AD4): PCM in over a WebSocket, MP3 out to a room through a ring buffer.

One session per room. The extension sends 48 kHz stereo s16le PCM to `/live/ingest?room=<uid>`; ffmpeg encodes it once to
MP3 320 CBR; a reader always drains ffmpeg's output into a bounded ring of whole MP3 frames (oldest dropped), so ffmpeg never
blocks. Each listener (the speaker, normally) starts a short burst behind the live edge and has its own bounded queue: one that
stops reading loses its own frames and never slows another.

Listeners are served by `LiveAwareProtocol`, a uvicorn protocol that takes over `GET /live/<id>.mp3` connections before uvicorn's
HTTP layer sees them. uvicorn answers an HTTP/1.1 request that has no length with `Transfer-Encoding: chunked`, which a speaker
would play as audio, so the response is written by hand: HTTP/1.0 style, `Connection: close`, no length, no ICY metadata.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import socket
import threading
from collections import deque
from typing import Callable

BITRATE = 320
SAMPLE_RATE = 48_000
FRAME_SECONDS = 1152 / SAMPLE_RATE                 # an MPEG-1 Layer III frame holds 1152 samples: 24 ms
BURST_FRAMES = 12                                  # what a new listener gets first: about 0.3 s behind the live edge
RING_FRAMES = 600                                  # 14 s of audio kept for listeners that join late
LISTENER_QUEUE_FRAMES = 400                        # 9.6 s: a listener this far behind starts losing its oldest frames
HEADER_TIMEOUT = 10.0                              # seconds a listener has to finish its request
FFMPEG_EXIT_TIMEOUT = 2.0
CLOSE_GRACE = 3.0                                 # seconds a finished listener gets to take its last bytes before the socket is cut
SEND_BUFFER = 65_536                               # per-listener kernel send buffer (about 1.6 s of audio)

_BITRATES = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0)
_RATES = (44_100, 48_000, 32_000, 0)
RESPONSE_HEAD = (b"HTTP/1.0 200 OK\r\nContent-Type: audio/mpeg\r\nConnection: close\r\nCache-Control: no-cache\r\n"
                 b"Pragma: no-cache\r\n\r\n")
PATH = re.compile(rb"/live/([A-Za-z0-9_-]{4,64})\.mp3")


class LiveError(Exception):
    pass


def frame_length(header: bytes | bytearray | memoryview) -> int | None:
    """The byte length of the MPEG-1 Layer III frame whose 4-byte header this is, or None if it isn't one."""
    if len(header) < 4 or header[0] != 0xFF or (header[1] & 0xFE) != 0xFA:       # sync, MPEG-1, Layer III
        return None
    bitrate = _BITRATES[header[2] >> 4]
    rate = _RATES[(header[2] >> 2) & 3]
    if not bitrate or not rate:
        return None
    return 144 * bitrate * 1000 // rate + ((header[2] >> 1) & 1)


class Mp3Framer:
    """Cuts a byte stream into whole MP3 frames, skipping anything that isn't one (a stray tag, a torn first frame)."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self.buf += data
        frames, pos = [], 0
        while pos + 4 <= len(self.buf):
            n = frame_length(self.buf[pos:pos + 4])
            if n is None:
                pos += 1
                continue
            if pos + n > len(self.buf):
                break
            frames.append(bytes(self.buf[pos:pos + n]))
            pos += n
        del self.buf[:pos]
        return frames


class Listener:
    """One connection reading a session: a bounded queue of frames, losing its own oldest when it falls behind."""

    def __init__(self, session: "LiveSession") -> None:
        self.session = session
        self.frames: deque[bytes] = deque()
        self.dropped = 0
        self.closed = False
        self.transport = None
        self.wake = asyncio.Event()

    def offer(self, frame: bytes) -> None:
        if len(self.frames) >= LISTENER_QUEUE_FRAMES:          # per-listener loss: a slow reader never holds anyone else up
            self.frames.popleft()
            self.dropped += 1
        self.frames.append(frame)
        self.wake.set()

    def close(self) -> None:
        self.closed = True
        self.wake.set()

    def abort(self) -> None:
        """Cut the connection now (a peer that stopped reading would hold it open for ever)."""
        if self.transport is not None:
            self.transport.abort()

    async def pump(self, transport, flow) -> None:
        """Write queued frames until the session ends and the queue is empty, or the connection goes."""
        while not self.closed and not transport.is_closing():
            if not self.frames:
                if self.session.ended:
                    return
                self.wake.clear()
                await self.wake.wait()
                continue
            data = b"".join(self.frames)
            self.frames.clear()
            transport.write(data)
            await flow.drain()                                  # waits while the peer isn't reading; frames pile up (and drop) meanwhile


class LiveSession:
    def __init__(self, registry: "LiveRegistry", room_uid: str, coordinator_uid: str, proc) -> None:
        self.registry = registry
        self.room_uid = room_uid
        self.coordinator_uid = coordinator_uid
        self.id = secrets.token_urlsafe(9)
        self.proc = proc
        self.ring: deque[bytes] = deque(maxlen=RING_FRAMES)
        self.framer = Mp3Framer()
        self.listeners: set[Listener] = set()
        self.ended: str | None = None                            # the reason, once over
        self.seen = False                                        # the room has reported playing our URL
        self.armed = False                                       # the room has been told to play it; only then can it "switch away"
        self.pinned = False                                      # the monitor is polling the room for us (see service.live_pin)
        self.pin_lock = threading.Lock()
        self.notify: Callable[[str], object] | None = None       # tells the ingest socket why the stream ended
        self.frames_out = 0
        self.detail: str | None = None                           # why, for the ingest side, when it is more than the reason says
        self._reader = asyncio.ensure_future(self._drain())

    # ---- audio in -------------------------------------------------------------------------------------------
    async def feed(self, pcm: bytes) -> None:
        if self.ended or self.proc.stdin is None or self.proc.stdin.is_closing():
            return
        try:
            self.proc.stdin.write(pcm)
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            await self.end("service-error")

    async def _drain(self) -> None:
        """Always read ffmpeg's output, so it never blocks on a full pipe, whatever the listeners do."""
        try:
            while True:
                chunk = await self.proc.stdout.read(8192)
                if not chunk:
                    break
                for frame in self.framer.feed(chunk):
                    self.frames_out += 1
                    self.ring.append(frame)
                    for listener in tuple(self.listeners):
                        listener.offer(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        if not self.ended:
            await self.end("service-error")                      # ffmpeg stopped by itself

    # ---- listeners --------------------------------------------------------------------------------------------
    def add_listener(self) -> Listener:
        listener = Listener(self)
        for frame in list(self.ring)[-BURST_FRAMES:]:            # whole frames, so the listener starts on a frame boundary
            listener.frames.append(frame)
        self.listeners.add(listener)
        listener.wake.set()
        return listener

    def remove_listener(self, listener: Listener) -> None:
        listener.close()
        self.listeners.discard(listener)

    # ---- ending ---------------------------------------------------------------------------------------------------
    async def end(self, reason: str) -> None:
        """Stop the stream (once): tell the ingest side why, stop ffmpeg, let listeners finish and close."""
        if self.ended:
            return
        self.ended = reason
        self.registry._forget(self)
        if self.notify is not None:
            try:
                result = self.notify(reason)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass
        loop = asyncio.get_running_loop()
        for listener in tuple(self.listeners):
            listener.wake.set()
            loop.call_later(CLOSE_GRACE, listener.abort)         # one that is still here then isn't taking its last bytes
        await self._stop_ffmpeg()
        if self.registry.on_end is not None:
            try:
                result = self.registry.on_end(self, reason)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass

    async def _stop_ffmpeg(self) -> None:
        proc = self.proc
        try:
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), FFMPEG_EXIT_TIMEOUT)
        except asyncio.TimeoutError:
            for stop in (proc.terminate, proc.kill):
                try:
                    stop()
                    await asyncio.wait_for(proc.wait(), FFMPEG_EXIT_TIMEOUT)
                    break
                except (asyncio.TimeoutError, ProcessLookupError):
                    continue
        if asyncio.current_task() is not self._reader:           # end() can be running on the reader itself (ffmpeg died)
            self._reader.cancel()


class LiveRegistry:
    """Live sessions by room UID. Everything here runs on the service's event loop."""

    def __init__(self, ffmpeg: Callable[[], str], *, host_ok: Callable[[str], bool] | None = None,
                 on_end: Callable[["LiveSession", str], object] | None = None,
                 coordinator_of: Callable[[str], str | None] | None = None) -> None:
        self.ffmpeg = ffmpeg
        self.coordinator_of = coordinator_of or (lambda room_uid: None)       # the room's group coordinator right now
        self._start_locks: dict[str, asyncio.Lock] = {}
        self.host_ok = host_ok or (lambda host: True)
        self.on_end = on_end                                      # called (awaited if async) once a session has ended, with its reason
        self.sessions: dict[str, LiveSession] = {}
        self.loop: asyncio.AbstractEventLoop | None = None

    def find(self, session_id: str) -> LiveSession | None:
        return next((s for s in self.sessions.values() if s.id == session_id and not s.ended), None)

    def _forget(self, session: LiveSession) -> None:
        if self.sessions.get(session.room_uid) is session:
            del self.sessions[session.room_uid]

    async def start(self, room_uid: str, coordinator_uid: str) -> LiveSession:
        """A new session for the room, replacing any running one."""
        self.loop = asyncio.get_running_loop()
        async with self._start_locks.setdefault(room_uid, asyncio.Lock()):      # two starts for one room must not both survive
            return await self._start(room_uid, coordinator_uid)

    async def _start(self, room_uid: str, coordinator_uid: str) -> LiveSession:
        old = self.sessions.get(room_uid)
        if old is not None:
            await old.end("replaced")
        try:
            proc = await asyncio.create_subprocess_exec(
                self.ffmpeg(), "-v", "error", "-fflags", "nobuffer", "-analyzeduration", "0", "-probesize", "32",     # raw PCM: no probing delay
                "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "2", "-i", "pipe:0",
                "-c:a", "libmp3lame", "-b:a", f"{BITRATE}k", "-reservoir", "0", "-write_xing", "0", "-id3v2_version", "0",
                "-flush_packets", "1", "-f", "mp3", "pipe:1",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        except OSError as e:
            raise LiveError(f"couldn't start ffmpeg: {e}") from None
        session = LiveSession(self, room_uid, coordinator_uid, proc)
        self.sessions[room_uid] = session
        return session

    async def end_all(self, reason: str = "service-error") -> None:
        for session in tuple(self.sessions.values()):
            await session.end(reason)

    # ---- the room's own state: a stream someone else replaced ends ------------------------------------------------------
    def on_state(self, coordinator_uid: str, old: dict | None, new: dict) -> None:
        """RoomMonitor subscriber. Runs on the polling thread, so hand over to the event loop."""
        if self.loop is None or not self.sessions:
            return
        try:
            self.loop.call_soon_threadsafe(self._check, coordinator_uid, new)
        except RuntimeError:                                     # the loop has closed
            pass

    def on_topology(self) -> None:
        """RoomMonitor subscriber: a room changed groups. A stream whose room is no longer led by the coordinator it started on
        isn't playing there any more. Runs on a polling thread."""
        if self.loop is None or not self.sessions:
            return
        try:
            self.loop.call_soon_threadsafe(self._check_groups)
        except RuntimeError:
            pass

    def _check_groups(self) -> None:
        for session in tuple(self.sessions.values()):
            now = self.coordinator_of(session.room_uid)
            if session.armed and not session.ended and now is not None and now != session.coordinator_uid:
                asyncio.ensure_future(session.end("room-switched"))

    def _check(self, coordinator_uid: str, new: dict) -> None:
        uri = ((new or {}).get("track") or {}).get("uri") or ""
        for session in tuple(self.sessions.values()):
            if session.ended:
                continue
            if f"/live/{session.id}.mp3" in uri:
                session.seen = True                              # whichever group reports it, and even while the room is still being told
            elif session.seen and session.armed and uri and session.coordinator_uid == coordinator_uid:
                asyncio.ensure_future(session.end("room-switched"))


# ---- the ingest WebSocket ---------------------------------------------------------------------------------------------

async def ingest(ws, registry: LiveRegistry, *, coordinator_uid_of: Callable[[str], str | None],
                 prepare: Callable[[str, "LiveSession"], object], play: Callable[[str, "LiveSession", str], object]) -> None:
    """`/live/ingest?room=<uid>`: binary frames are PCM, text frames are control JSON ({"end": "stop"|"tab-closed"}).

    `started` is sent as soon as the stream exists, and the room is told to play it alongside: a real speaker answers that request only
    once it has audio to buffer, and the audio can't flow until the client has been told to start."""
    room = ws.query_params.get("room", "")
    coordinator_uid = coordinator_uid_of(room) if room else None
    if coordinator_uid is None:
        await ws.close(code=4404, reason="no such room")
        return
    await ws.accept()
    try:
        session = await registry.start(room, coordinator_uid)
    except LiveError as e:
        await ws.send_text(json.dumps({"ended": "service-error", "detail": str(e)}))
        await ws.close()
        return

    async def tell(reason: str) -> None:
        await ws.send_text(json.dumps({"ended": reason, **({"detail": session.detail} if session.detail else {})}))
        await ws.close()

    session.notify = tell
    reason = "stop"
    playing = None
    try:
        url = await prepare(room, session)
        await ws.send_text(json.dumps({"started": True, "id": session.id, "url": url}))
        playing = asyncio.ensure_future(play(room, session, url))

        def played(task: "asyncio.Future") -> None:
            if task.cancelled() or task.exception() is None:
                return
            session.detail = str(task.exception())[:200] or task.exception().__class__.__name__
            asyncio.ensure_future(session.end("service-error"))

        playing.add_done_callback(played)
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes"):
                await session.feed(msg["bytes"])
            elif msg.get("text"):
                try:
                    control = json.loads(msg["text"])
                except ValueError:
                    continue
                if isinstance(control, dict) and control.get("end") in ("stop", "tab-closed"):
                    reason = control["end"]
                    break
    except Exception as e:
        reason = "service-error"
        session.detail = str(e)[:200] or e.__class__.__name__
    finally:
        session.notify = None if reason != "service-error" else session.notify
        await session.end(reason)
        if playing is not None and not playing.done():
            playing.cancel()
        try:
            await ws.close()
        except Exception:
            pass


# ---- listeners: GET /live/<id>.mp3, written by hand ------------------------------------------------------------------------

class LiveConnection:
    """One hijacked connection: read the request head, then stream (or answer 404/403/405)."""

    def __init__(self, protocol, registry: LiveRegistry) -> None:
        self.protocol = protocol
        self.registry = registry
        self.buf = bytearray()
        self.listener: Listener | None = None
        self.started = False
        self.timer = asyncio.get_running_loop().call_later(HEADER_TIMEOUT, self._timeout)

    def _timeout(self) -> None:
        if not self.started:
            self.protocol.transport.close()

    def data_received(self, data: bytes) -> None:
        if self.started:
            return                                              # a listener has nothing more to say
        self.buf += data
        end = self.buf.find(b"\r\n\r\n")
        if end < 0:
            if len(self.buf) > 16_384:
                self._answer(b"431 Request Header Fields Too Large")
            return
        self.started = True
        self.timer.cancel()
        asyncio.ensure_future(self._serve(bytes(self.buf[:end]).decode("latin1")))

    def _answer(self, status: bytes) -> None:
        self.started = True
        t = self.protocol.transport
        t.write(b"HTTP/1.0 " + status + b"\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
        t.close()

    async def _serve(self, head: str) -> None:
        t = self.protocol.transport
        lines = head.split("\r\n")
        parts = lines[0].split(" ")
        headers = {}
        for line in lines[1:]:                                  # names arrive in mixed case (Sonos sends CONNECTION, HOST)
            name, sep, value = line.partition(":")
            if sep:
                headers.setdefault(name.strip().lower(), value.strip())
        if len(parts) < 2 or parts[0] not in ("GET", "HEAD"):
            return self._answer(b"405 Method Not Allowed")
        if not self.registry.host_ok(headers.get("host", "")):
            return self._answer(b"403 Forbidden")
        m = PATH.fullmatch(parts[1].split("?", 1)[0].encode("latin1"))
        session = self.registry.find(m.group(1).decode()) if m else None
        if session is None:
            return self._answer(b"404 Not Found")
        sock = t.get_extra_info("socket")
        if sock is not None:
            try:                                                # a stream is 40 KB/s: a small kernel buffer is plenty, and keeps a stalled
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SEND_BUFFER)     # reader's backlog in our bounded queue instead
            except OSError:
                pass
        t.write(RESPONSE_HEAD)
        if parts[0] == "HEAD":
            t.close()
            return
        self.listener = session.add_listener()
        self.listener.transport = t
        try:
            await self.listener.pump(t, self.protocol.flow)
        finally:
            session.remove_listener(self.listener)
            t.close()

    def lost(self) -> None:
        self.timer.cancel()
        if self.listener is not None:
            self.listener.close()


def classify_request(buf: bytes) -> str | None:
    """"live" for `GET|HEAD /live/<id>…`, "normal" for anything else (including the `/live/ingest` upgrade), None if too short to tell."""
    for method in (b"GET", b"HEAD"):
        head = method + b" /live/"
        if buf.startswith(head):
            rest = buf[len(head):]
            if rest.startswith(b"ingest"):
                nxt = rest[6:7]
                if not nxt:
                    return None
                return "normal" if nxt in (b"?", b" ", b"/") else "live"
            return None if b"ingest".startswith(rest) else "live"
        if head.startswith(buf):
            return None
    return "normal"


def protocol_for(registry: LiveRegistry):
    """A uvicorn HTTP protocol class that serves `GET|HEAD /live/<id>.mp3` from `registry` and leaves every other request to uvicorn."""
    from uvicorn.protocols.http.httptools_impl import HttpToolsProtocol

    class LiveAwareProtocol(HttpToolsProtocol):
        _mode: object = None                                    # None: undecided; False: ordinary HTTP; a LiveConnection: ours
        _held = b""

        def data_received(self, data: bytes) -> None:
            if self._mode is False:
                return super().data_received(data)
            if self._mode is None:
                buf = self._held + data
                kind = classify_request(buf)
                if kind is None:
                    self._held = buf                            # too short to tell yet
                    return
                self._held = b""
                if kind == "live":
                    self._mode = LiveConnection(self, registry)
                    return self._mode.data_received(buf)
                self._mode = False
                return super().data_received(buf)
            self._mode.data_received(data)

        def shutdown(self) -> None:
            if isinstance(self._mode, LiveConnection):          # a listener never finishes by itself: cut it so the server can exit
                self._mode.lost()
                self.transport.abort()
                return
            super().shutdown()

        def connection_lost(self, exc) -> None:
            if isinstance(self._mode, LiveConnection):
                self._mode.lost()
            super().connection_lost(exc)

    return LiveAwareProtocol
