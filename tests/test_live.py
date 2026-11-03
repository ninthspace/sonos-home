"""Live stream sessions (spec FR16, AD4): PCM in over the ingest WebSocket, MP3 out to listeners, with real sockets and ffmpeg."""

import json
import math
import os
import socket
import struct
import subprocess
import threading
import time

import pytest
import uvicorn
from websockets.sync.client import connect

from sonos_cli import config, live, service
from sonos_cli.fakes import FakeSystem
from sonos_cli.library import Library
from sonos_cli.rooms import RoomMonitor
from sonos_cli.server import local_ip_towards

FRAME = 960                                                  # bytes of one 320 kbps / 48 kHz MPEG-1 Layer III frame
MP3_BYTES_PER_SECOND = 40_000
RATE = 48_000


def tone(seconds: float, freq: int = 440) -> bytes:
    """48 kHz stereo s16le sine. A whole number of cycles per second keeps repeated chunks continuous."""
    n = int(RATE * seconds)
    return b"".join(struct.pack("<hh", s, s) for s in (int(12000 * math.sin(2 * math.pi * freq * i / RATE)) for i in range(n)))


TONE_1S = tone(1)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Live:
    """A real uvicorn server (with the live protocol) over fake rooms."""

    def __init__(self, tmp_path):
        self.port = free_port()
        self.system = FakeSystem()
        self.monitor = RoomMonitor(self.system.discover)
        self.monitor.refresh()
        cfg = config.Config(path=tmp_path / "service" / "config.toml")
        self.app = service.create_app(cfg, library=Library.create([]), monitor=self.monitor, port=self.port,
                                      allowed_hosts={"127.0.0.1", "localhost"})
        self.registry = self.app.live
        server_config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning", http=live.protocol_for(self.registry))
        self.server = uvicorn.Server(server_config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started and time.time() < deadline:
            time.sleep(0.02)
        assert self.server.started

    def close(self):
        self.server.should_exit = True
        self.thread.join(10)

    @property
    def room(self) -> str:
        return self.system.office.uid

    def ingest(self, room=None, wait=True):
        """Connect; `started` arrives at once, and the room is told to play the stream alongside. By default wait until it has been
        (or the session has ended), which is what most tests want to start from."""
        ws = connect(f"ws://127.0.0.1:{self.port}/live/ingest?room={room or self.room}", origin=f"http://127.0.0.1:{self.port}", open_timeout=10, legacy=True)
        started = json.loads(ws.recv(timeout=10))
        if wait and started.get("started"):
            end = time.time() + 5
            while time.time() < end:
                session = next((x for x in self.registry.sessions.values() if x.id == started["id"]), None)
                if session is None or session.armed or session.ended:
                    break
                time.sleep(0.02)
        return ws, started

    def listen(self, session_id: str, rcvbuf: int | None = None, method: str = "GET") -> socket.socket:
        s = socket.socket()
        if rcvbuf:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        s.settimeout(10)
        s.connect(("127.0.0.1", self.port))
        s.sendall(f"{method} /live/{session_id}.mp3 HTTP/1.1\r\nHOST: 127.0.0.1:{self.port}\r\nIcy-MetaData: 1\r\nCONNECTION: close\r\n\r\n".encode())
        return s

    def session(self):
        return next(iter(self.registry.sessions.values()))


@pytest.fixture
def lv(tmp_path):
    handle = Live(tmp_path)
    yield handle
    handle.close()


def read_head(s: socket.socket) -> tuple[str, bytes]:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = s.recv(4096)
        if not chunk:
            break
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    return head.decode("latin1"), rest


def read_for(s: socket.socket, seconds: float = None, want: int = None, first: bytes = b"") -> bytes:
    """Body bytes until `want` have arrived or `seconds` have passed or the server closes."""
    out, deadline = bytearray(first), time.time() + (seconds or 15)
    s.settimeout(0.5)
    while time.time() < deadline and (want is None or len(out) < want):
        try:
            chunk = s.recv(65536)
        except socket.timeout:
            continue
        if not chunk:
            break
        out += chunk
    return bytes(out)


def feed(ws, seconds: float) -> None:
    for _ in range(int(seconds)):
        ws.send(TONE_1S)
    rest = seconds - int(seconds)
    if rest:
        ws.send(tone(rest))


def feed_live(ws, seconds: float) -> None:
    """The way the extension sends: 100 ms of audio every 100 ms."""
    chunk, t0 = tone(0.1), time.time()
    for i in range(int(seconds * 10)):
        ws.send(chunk)
        time.sleep(max(0, t0 + (i + 1) * 0.1 - time.time()))


def decode(mp3: bytes) -> tuple[int, str, list[int]]:
    """(ffmpeg's exit code, its stderr at error level, the mono samples)."""
    r = subprocess.run([config.binary("ffmpeg"), "-v", "error", "-f", "mp3", "-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", str(RATE), "pipe:1"],
                       input=mp3, capture_output=True)
    samples = list(struct.unpack(f"<{len(r.stdout) // 2}h", r.stdout[:len(r.stdout) // 2 * 2]))
    return r.returncode, r.stderr.decode(errors="replace"), samples


def frequency(samples: list[int]) -> float:
    mid = samples[RATE // 2: RATE // 2 + RATE * 2]                    # skip the encoder's lead-in; two seconds are plenty
    crossings = sum(1 for a, b in zip(mid, mid[1:]) if a < 0 <= b)
    return crossings / (len(mid) / RATE) if mid else 0.0


def wait_for(cond, seconds=5.0, what="condition"):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def children_named(name: str) -> list[str]:
    r = subprocess.run(["pgrep", "-P", str(os.getpid()), "-f", name], capture_output=True, text=True)
    return r.stdout.split()


# ---- unit: framing -----------------------------------------------------------------------------------------------

HEADER_320_48 = bytes([0xFF, 0xFB, 0xE4, 0x00])               # MPEG-1 Layer III, 320 kbps, 48 kHz, no padding


def test_frame_length_of_the_stream_we_make_and_of_things_that_are_not_frames():
    assert live.frame_length(HEADER_320_48) == FRAME
    assert live.frame_length(bytes([0xFF, 0xFB, 0xE6, 0x00])) == FRAME + 1               # padding bit
    for bad in (b"", b"\xff\xfb", b"ID3\x04", bytes([0xFF, 0xFB, 0xF4, 0x00]), bytes([0xFF, 0xFB, 0x04, 0x00]), bytes([0xFF, 0xE3, 0xE4, 0x00])):
        assert live.frame_length(bad) is None


def test_the_framer_returns_whole_frames_only_and_resyncs_after_junk():
    frame = HEADER_320_48 + bytes(FRAME - 4)
    f = live.Mp3Framer()
    assert f.feed(frame[:500]) == []                                   # half a frame: wait
    out = f.feed(frame[500:] + frame + frame[:100])
    assert out == [frame, frame] and bytes(f.buf) == frame[:100]
    g = live.Mp3Framer()
    assert g.feed(b"junk" + frame + b"\x00\x01" + frame + HEADER_320_48) == [frame, frame]


# ---- integration -------------------------------------------------------------------------------------------------

def test_pcm_in_comes_out_as_decodable_440_hz_mp3_and_the_room_is_told_to_play_it(lv):
    ws, started = lv.ingest()
    sid = started["id"]
    url = f"x-rincon-mp3radio://{local_ip_towards(lv.system.office.ip_address)}:{lv.port}/live/{sid}.mp3"
    assert started["url"] == url and ("play_uri", url) in lv.system.office.calls
    s = lv.listen(sid)
    head, rest = read_head(s)
    assert head.startswith("HTTP/1.0 200")
    feed(ws, 4)
    body = read_for(s, 10, want=int(3.5 * MP3_BYTES_PER_SECOND), first=rest)
    code, err, samples = decode(body)
    assert code == 0 and err == "" and abs(frequency(samples) - 440) <= 5, (code, err, frequency(samples))
    ws.close()


def test_a_listener_joining_ten_seconds_in_starts_on_a_frame_and_decodes_cleanly(lv):
    ws, started = lv.ingest()
    feed(ws, 10)
    wait_for(lambda: lv.session().frames_out >= 380, 10, "ten seconds of audio to be encoded")
    s = lv.listen(started["id"])
    head, rest = read_head(s)
    sender = threading.Thread(target=feed, args=(ws, 2), daemon=True)
    sender.start()
    body = read_for(s, 10, want=40 * FRAME, first=rest)
    sender.join()
    assert body[0] == 0xFF and (body[1] & 0xFE) == 0xFA and live.frame_length(body[:4]) == FRAME
    code, err, samples = decode(body)
    assert code == 0 and err == "" and len(samples) > RATE // 4, (code, err)
    ws.close()


def test_a_listener_that_never_reads_does_not_slow_a_second_one(lv):
    ws, started = lv.ingest()
    stuck = lv.listen(started["id"], rcvbuf=2048)                      # reads nothing, ever
    good = lv.listen(started["id"])
    head, rest = read_head(good)
    got = bytearray(rest)
    stop = threading.Event()

    def reader():
        good.settimeout(0.2)
        while not stop.is_set():
            try:
                chunk = good.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                break
            got.extend(chunk)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    started_at = time.time()
    feed(ws, 30)
    wait_for(lambda: lv.session().frames_out >= 30 * 41, 20, "30 s of audio to be encoded")
    time.sleep(1.0)
    stop.set()
    t.join()
    produced = lv.session().frames_out * FRAME
    assert len(got) >= 0.95 * produced                                  # the second listener got (nearly) everything, as it was made
    listeners = list(lv.session().listeners)
    assert max(l.dropped for l in listeners) > 0                        # the stuck one lost its own oldest frames
    assert max(len(l.frames) for l in listeners) <= live.LISTENER_QUEUE_FRAMES
    stuck.close()
    ws.close()


def test_the_response_has_no_chunking_length_or_icy_metadata(lv):
    ws, started = lv.ingest()
    s = lv.listen(started["id"])
    head, rest = read_head(s)
    lines = [l.lower() for l in head.split("\r\n")]
    assert lines[0].startswith("http/1.0 200") and "content-type: audio/mpeg" in lines
    assert not any(l.startswith(("transfer-encoding", "content-length", "icy-")) for l in lines)
    feed(ws, 1)
    body = read_for(s, 5, want=2 * FRAME, first=rest)
    assert body[:1] == b"\xff"                                          # raw frames straight after the head: nothing chunk-framed
    ws.close()


def test_a_listener_that_disconnects_can_reconnect_and_hear_audio_again(lv):
    ws, started = lv.ingest()
    sender = threading.Thread(target=feed_live, args=(ws, 5), daemon=True)
    sender.start()
    first = lv.listen(started["id"])
    head, rest = read_head(first)
    assert len(read_for(first, 5, want=20_000, first=rest)) >= 20_000
    first.close()
    time.sleep(1.0)
    second = lv.listen(started["id"])
    head, rest = read_head(second)
    body = read_for(second, 8, want=30 * FRAME, first=rest)
    code, err, samples = decode(body)
    assert head.startswith("HTTP/1.0 200") and len(body) >= 30 * FRAME and code == 0 and err == "", (len(body), code, err)
    sender.join()
    ws.close()


def rss_kb() -> int:
    return int(subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True).stdout.strip())


def test_sixty_seconds_of_ingest_with_no_listener_stays_within_the_buffer_and_ten_megabytes(lv):
    ws, started = lv.ingest()
    feed(ws, 2)
    wait_for(lambda: lv.session().frames_out > 40, 10, "the encoder to start")
    before = rss_kb()
    feed(ws, 60)
    wait_for(lambda: lv.session().frames_out >= 62 * 41, 30, "the audio to be encoded")
    session = lv.session()
    assert len(session.ring) == live.RING_FRAMES                           # full and bounded
    assert (rss_kb() - before) < 10_000                                  # kB
    ws.close()


def test_when_ingest_closes_open_listeners_end_and_new_requests_are_404(lv):
    ws, started = lv.ingest()
    s = lv.listen(started["id"])
    head, rest = read_head(s)
    feed(ws, 1)
    ws.close()
    began = time.time()
    tail = read_for(s, 5)                                                # returns when the server closes, or after 5 s
    assert time.time() - began < 5
    again = lv.listen(started["id"])
    assert read_head(again)[0].startswith("HTTP/1.0 404")


def test_a_room_that_moves_to_another_source_ends_the_stream_and_leaves_no_ffmpeg(lv):
    ws, started = lv.ingest()
    feed(ws, 1)
    session = lv.session()
    lv.monitor.refresh_group_of(lv.room)
    wait_for(lambda: session.seen, 5, "the room to be seen playing the stream")
    proc = session.proc
    lv.system.office.set_source("x-sonos-spotify:spotify%3atrack%3a123")
    lv.monitor.refresh_group_of(lv.room)
    assert json.loads(ws.recv(timeout=5)) == {"ended": "room-switched"}
    wait_for(lambda: proc.returncode is not None, 5, "ffmpeg to exit")
    assert children_named("ffmpeg") == []
    assert lv.registry.sessions == {}
    assert lv.system.office.source_uri.startswith("x-sonos-spotify")        # and the room was left where it went


def test_a_second_stream_for_the_same_room_replaces_the_first(lv):
    ws1, first = lv.ingest()
    old = lv.session()
    ws2, second = lv.ingest()
    assert first["id"] != second["id"]
    assert json.loads(ws1.recv(timeout=5)) == {"ended": "replaced"}
    wait_for(lambda: old.proc.returncode is not None, 5, "the first ffmpeg to exit")
    assert len(lv.registry.sessions) == 1 and lv.session().id == second["id"]
    assert len(children_named("ffmpeg")) == 1
    ws2.close()


@pytest.mark.parametrize("reason", ["stop", "tab-closed"])
def test_a_stop_from_the_extension_ends_the_stream_and_stops_the_room(lv, reason):
    ws, started = lv.ingest()
    feed(ws, 1)
    lv.monitor.refresh_group_of(lv.room)
    assert lv.system.office.transport_state == "PLAYING"
    ws.send(json.dumps({"end": reason}))
    wait_for(lambda: lv.registry.sessions == {}, 5, "the session to end")
    wait_for(lambda: lv.system.office.transport_state != "PLAYING", 5, "the room to stop")


def test_an_unknown_room_and_a_missing_ffmpeg_are_refused_cleanly(lv):
    with pytest.raises(Exception):
        connect(f"ws://127.0.0.1:{lv.port}/live/ingest?room=nope", origin=f"http://127.0.0.1:{lv.port}", open_timeout=5, legacy=True).recv(timeout=5)
    lv.registry.ffmpeg = lambda: "/nonexistent/ffmpeg"
    ws = connect(f"ws://127.0.0.1:{lv.port}/live/ingest?room={lv.room}", origin=f"http://127.0.0.1:{lv.port}", open_timeout=5, legacy=True)
    assert json.loads(ws.recv(timeout=5))["ended"] == "service-error"
    assert lv.registry.sessions == {}


def test_other_requests_on_the_same_port_are_untouched_and_a_split_first_packet_still_reaches_the_live_handler(lv):
    import httpx
    assert httpx.get(f"http://127.0.0.1:{lv.port}/api/health", timeout=5).status_code == 200
    s = socket.create_connection(("127.0.0.1", lv.port), timeout=5)
    s.sendall(b"GET /li")
    time.sleep(0.2)
    s.sendall(b"ve/nosuchsession.mp3 HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
    assert read_head(s)[0].startswith("HTTP/1.0 404")
    s2 = socket.create_connection(("127.0.0.1", lv.port), timeout=5)
    s2.sendall(b"GET /live/abcd.mp3 HTTP/1.1\r\nHost: evil.example\r\n\r\n")
    assert read_head(s2)[0].startswith("HTTP/1.0 403")                  # the Host check applies here too


def test_a_listener_that_never_finishes_its_request_is_dropped_and_an_oversized_head_is_refused(lv, monkeypatch):
    monkeypatch.setattr(live, "HEADER_TIMEOUT", 0.5)
    quiet = socket.create_connection(("127.0.0.1", lv.port), timeout=5)
    quiet.sendall(b"GET /live/abcdef.mp3 HTTP/1.1\r\nHost: 127.0.0.1\r\n")             # never ends the head
    began = time.time()
    assert quiet.recv(100) == b"" and time.time() - began < 3                           # the server closed it
    big = socket.create_connection(("127.0.0.1", lv.port), timeout=5)
    big.sendall(b"GET /live/abcdef.mp3 HTTP/1.1\r\n" + b"X-Pad: " + b"a" * 20_000)
    assert read_head(big)[0].startswith("HTTP/1.0 431")


def test_head_gets_the_headers_and_no_body_and_other_methods_are_refused(lv):
    ws, started = lv.ingest()
    s = lv.listen(started["id"], method="HEAD")
    head, rest = read_head(s)
    assert head.startswith("HTTP/1.0 200") and "audio/mpeg" in head and read_for(s, 2, first=rest) == b""
    assert lv.session().listeners == set()
    post = socket.create_connection(("127.0.0.1", lv.port), timeout=5)
    post.sendall(b"POST /live/" + started["id"].encode() + b".mp3 HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 0\r\n\r\n")
    assert read_head(post)[0].startswith(("HTTP/1.1 4", "HTTP/1.0 4"))                  # not a stream: uvicorn answers (no such route)
    ws.close()


def test_the_ingest_ignores_empty_and_garbled_messages_and_a_stream_started_but_never_fed_just_ends(lv):
    ws, started = lv.ingest()
    ws.send(b"")
    ws.send("not json")
    ws.send(json.dumps({"end": "something-else"}))
    ws.send(json.dumps(["a", "list"]))
    time.sleep(0.3)
    assert lv.session().ended is None                                                 # still running
    feed(ws, 1)
    wait_for(lambda: lv.session().frames_out > 10, 5, "audio after the noise")
    ws.close()
    wait_for(lambda: lv.registry.sessions == {}, 5, "the session to end")


# ---- what the audit found ---------------------------------------------------------------------------------------------

def test_a_stop_from_the_extension_does_not_stop_a_room_that_has_already_moved_on(lv):
    ws, started = lv.ingest()
    feed(ws, 1)
    lv.monitor.refresh_group_of(lv.room)                              # the cache says the room plays our stream ...
    lv.system.office.set_source("x-sonos-spotify:spotify%3atrack%3a1")   # ... and then it moves on, with no poll in between
    lv.system.office.calls.clear()
    ws.send(json.dumps({"end": "stop"}))
    wait_for(lambda: lv.registry.sessions == {}, 5, "the session to end")
    time.sleep(0.5)
    assert ("stop",) not in lv.system.office.calls and lv.system.office.transport_state == "PLAYING"


def test_when_ffmpeg_dies_by_itself_the_extension_is_told_and_the_room_is_stopped(lv):
    ws, started = lv.ingest()
    feed(ws, 1)
    lv.monitor.refresh_group_of(lv.room)
    session = lv.session()
    session.proc.kill()
    assert json.loads(ws.recv(timeout=5))["ended"] == "service-error"
    wait_for(lambda: lv.system.office.transport_state != "PLAYING", 5, "the room to stop")
    assert lv.registry.sessions == {}


def test_two_starts_for_one_room_at_once_leave_one_session_and_no_orphan_ffmpeg():
    import asyncio

    async def go():
        reg = live.LiveRegistry(lambda: config.binary("ffmpeg"))
        a, b = await asyncio.gather(reg.start("R", "R"), reg.start("R", "R"))
        survivors = list(reg.sessions.values())
        await reg.end_all()
        return a, b, survivors

    a, b, survivors = asyncio.run(go())
    assert len(survivors) == 1
    loser = a if survivors[0] is b else b
    assert loser.ended == "replaced" and loser.proc.returncode is not None


def test_a_room_that_joins_another_group_ends_its_stream(lv):
    ws, started = lv.ingest()
    feed(ws, 1)
    session = lv.session()
    lv.monitor.refresh_group_of(lv.room)
    wait_for(lambda: session.seen, 5, "the room to be seen playing the stream")
    lv.system.office.join(lv.system.kitchen)                          # the room goes to listen to Kitchen's group
    lv.monitor.refresh_group_of(lv.room)
    assert json.loads(ws.recv(timeout=5)) == {"ended": "room-switched"}
    wait_for(lambda: session.proc.returncode is not None, 5, "ffmpeg to exit")


def test_the_server_can_shut_down_while_a_listener_has_stopped_reading(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "CLOSE_GRACE", 60)                         # so only the protocol's own shutdown can free the server
    handle = Live(tmp_path)
    try:
        ws, started = handle.ingest()
        stuck = handle.listen(started["id"], rcvbuf=2048)             # reads nothing
        feed(ws, 20)
        wait_for(lambda: handle.session().frames_out >= 20 * 41, 15, "the audio to be encoded")
        assert handle.session().listeners
    finally:
        began = time.time()
        handle.server.should_exit = True
        handle.thread.join(10)
        assert not handle.thread.is_alive() and time.time() - began < 8
        stuck.close()


def test_a_listener_of_a_stream_that_has_ended_is_cut_off_if_it_will_not_take_the_rest(lv, monkeypatch):
    monkeypatch.setattr(live, "CLOSE_GRACE", 0.5)
    ws, started = lv.ingest()
    stuck = lv.listen(started["id"], rcvbuf=2048)                       # reads nothing
    feed(ws, 20)
    wait_for(lambda: lv.session().frames_out >= 20 * 41, 15, "the audio to be encoded")
    listener = next(iter(lv.session().listeners))
    ws.close()
    wait_for(lambda: lv.registry.sessions == {}, 5, "the session to end")
    wait_for(lambda: listener.closed, 4, "the server to cut the stuck listener")                # while the client still hasn't read a byte
    stuck.close()


def test_pcm_sent_in_messages_that_split_stereo_samples_still_decodes_to_the_same_tone(lv):
    ws, started = lv.ingest()
    s = lv.listen(started["id"])
    head, rest = read_head(s)
    data = tone(4)
    for i in range(0, len(data), 1001):                               # 1001 is odd: never a whole number of stereo samples
        ws.send(data[i:i + 1001])
    body = read_for(s, 10, want=int(3.5 * MP3_BYTES_PER_SECOND), first=rest)
    code, err, samples = decode(body)
    assert code == 0 and abs(frequency(samples) - 440) <= 5, (code, err, frequency(samples))
    ws.close()


def test_a_refused_start_tells_the_extension_why(lv):
    def refuse(*a, **k):
        raise RuntimeError("fake: the speaker said no")

    lv.system.office.play_uri = refuse
    ws, started = lv.ingest(wait=False)
    assert started["started"] is True                                  # the client is told to start at once ...
    msg = json.loads(ws.recv(timeout=5))                               # ... and learns a moment later that the speaker refused
    assert msg["ended"] == "service-error" and "the speaker said no" in msg["detail"]
    wait_for(lambda: lv.registry.sessions == {}, 5, "the session to end")


def test_streaming_to_a_room_the_monitor_still_thinks_leads_its_group_starts_and_keeps_running(lv):
    lv.system.office.join(lv.system.kitchen)                            # done in the Sonos app: the monitor's cache hasn't heard yet
    ws, started = lv.ingest()
    assert started["started"] is True                                   # not an immediate `room-switched`
    session = lv.session()
    feed(ws, 1)
    time.sleep(1.0)
    assert session.ended is None and session.coordinator_uid == lv.system.kitchen.uid
    assert any(c[0] == "play_uri" and "/live/" in c[1] for c in lv.system.kitchen.calls)      # the group's real coordinator was told
    ws.close()
    wait_for(lambda: lv.registry.sessions == {}, 5, "the session to end")


def test_a_start_that_is_replaced_while_the_speaker_is_still_being_told_leaves_no_pin_behind(lv):
    real = lv.system.office.play_uri

    def slow(*a, **k):
        time.sleep(1.0)
        return real(*a, **k)

    lv.system.office.play_uri = slow
    first = threading.Thread(target=lambda: lv.ingest(), daemon=True)
    first.start()
    time.sleep(0.3)
    ws2, second = lv.ingest()                                           # replaces the first while its play_uri is in flight
    first.join(10)
    assert lv.monitor._pinned == {lv.room: 1}                           # exactly the survivor's pin
    ws2.send(json.dumps({"end": "stop"}))
    wait_for(lambda: lv.registry.sessions == {}, 5, "the session to end")
    wait_for(lambda: lv.monitor._pinned == {}, 5, "the pin to be released")


def test_started_and_audio_flow_while_the_speaker_is_still_being_told_to_play(lv):
    release = threading.Event()
    real = lv.system.office.play_uri

    def blocked(*a, **k):                               # a real speaker answers this only once it has audio to buffer
        release.wait(10)
        return real(*a, **k)

    lv.system.office.play_uri = blocked
    began = time.time()
    ws, started = lv.ingest(wait=False)
    assert started["started"] is True and time.time() - began < 3             # not held up by the speaker
    session = lv.session()
    s = lv.listen(started["id"])
    head, rest = read_head(s)
    feed(ws, 2)
    body = read_for(s, 8, want=2 * MP3_BYTES_PER_SECOND // 2, first=rest)
    assert len(body) >= MP3_BYTES_PER_SECOND // 2 and not release.is_set() and not session.armed         # audio flowed before the speaker answered
    release.set()
    wait_for(lambda: session.armed, 5, "the room to be told")
    assert ("play_uri", started["url"]) in lv.system.office.calls
    ws.close()
