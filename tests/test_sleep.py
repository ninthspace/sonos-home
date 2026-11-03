"""The sleep-prevention assertion (spec FR17)."""

import pytest

from fakes import FakeSystem
from sonos_cli import config
from sonos_cli.rooms import RoomMonitor
from sonos_cli.sleep import SleepAssertion
from test_rooms import Clock, OUR_IP, play_url

OURS = f"http://{OUR_IP}:8737/media/abc/a.flac"


class FakeProc:
    def __init__(self, cmd):
        self.cmd = cmd
        self.terminated = False
        self.dead = False

    def terminate(self):
        self.terminated = True

    def poll(self):
        return 0 if (self.terminated or self.dead) else None


class Spawner:
    def __init__(self):
        self.procs = []

    def __call__(self, cmd):
        self.procs.append(FakeProc(cmd))
        return self.procs[-1]

    @property
    def running(self):
        return [p for p in self.procs if p.poll() is None]


@pytest.fixture
def world():
    system, clock, spawner = FakeSystem(), Clock(), Spawner()
    monitor = RoomMonitor(system.discover, our_hosts=lambda: {OUR_IP}, our_port=8737, clock=clock)
    monitor.refresh()
    sleep = SleepAssertion(monitor, grace_minutes=30, clock=clock, spawn=spawner, pid=4242)
    return type("W", (), dict(system=system, clock=clock, spawner=spawner, monitor=monitor, sleep=sleep))


def poll(w, device):
    w.monitor.refresh_group_of(device.uid)


def test_held_while_a_room_plays_our_url_with_one_caffeinate_for_the_service(world):
    play_url(world.system.lounge, OURS)
    poll(world, world.system.lounge)
    assert world.sleep.held and [p.cmd for p in world.spawner.running] == [["caffeinate", "-i", "-w", "4242"]]
    play_url(world.system.bedroom, OURS)                               # a second room of ours: still one process
    poll(world, world.system.bedroom)
    assert len(world.spawner.running) == 1 and len(world.spawner.procs) == 1


def test_not_held_for_another_hosts_url_a_remote_url_a_spotify_source_or_a_stopped_room(world):
    s = world.system
    play_url(s.lounge, "http://10.0.0.9:8737/media/abc/a.flac")        # another Mac's service
    s.bedroom.play_uri("https://radio.example.com/stream.mp3")          # a remote https stream, not proxied by us
    s.office.play_uri("x-sonos-vli:abc,spotify:xyz")
    for d in (s.lounge, s.bedroom, s.office):
        poll(world, d)
    assert not world.sleep.held and world.spawner.procs == []


def test_a_grouped_member_does_not_count_for_itself(world):
    s = world.system
    play_url(s.lounge, "http://10.0.0.9:8737/media/abc/a.flac")        # the group plays another host's audio
    s.kitchen.join(s.lounge)
    s.lounge.play_uri("http://10.0.0.9:8737/live/t.mp3")
    world.monitor.refresh()
    poll(world, s.kitchen)                                              # Kitchen reports x-rincon:… and follows Lounge
    assert not world.sleep.held and world.spawner.procs == []


def test_a_grouped_room_playing_ours_holds_the_assertion_once(world):
    s = world.system
    s.kitchen.join(s.lounge)
    play_url(s.lounge, OURS)
    world.monitor.refresh()
    poll(world, s.kitchen)
    assert world.sleep.held and len(world.spawner.procs) == 1


def test_held_while_paused_within_the_grace_period_and_released_after_it(world):
    s = world.system
    play_url(s.lounge, OURS)
    poll(world, s.lounge)
    s.lounge.pause()
    poll(world, s.lounge)
    assert world.sleep.held
    world.clock.advance(29 * 60)
    assert world.sleep.check() is True
    world.clock.advance(2 * 60)                                          # 31 minutes paused
    assert world.sleep.check() is False and not world.spawner.running


def test_resuming_after_a_long_pause_holds_again_and_restarts_the_grace_clock(world):
    s = world.system
    play_url(s.lounge, OURS)
    s.lounge.pause()
    poll(world, s.lounge)
    world.clock.advance(31 * 60)
    assert world.sleep.check() is False
    s.lounge.play()
    poll(world, s.lounge)
    assert world.sleep.held
    s.lounge.pause()
    poll(world, s.lounge)
    world.clock.advance(10 * 60)
    assert world.sleep.check() is True


def test_released_within_30_seconds_of_stopped(world):
    s = world.system
    play_url(s.lounge, OURS)
    poll(world, s.lounge)
    assert world.sleep.held
    s.lounge.stop()
    poll(world, s.lounge)
    world.clock.advance(29)
    assert not world.sleep.held and world.sleep.check() is False
    assert world.spawner.procs[0].terminated


def test_released_when_the_room_is_switched_to_another_source(world):
    s = world.system
    play_url(s.lounge, OURS)
    poll(world, s.lounge)
    s.lounge.play_uri("x-sonos-vli:abc,spotify:xyz")
    poll(world, s.lounge)
    assert not world.sleep.held


def test_a_caffeinate_that_died_is_started_again(world):
    play_url(world.system.lounge, OURS)
    poll(world, world.system.lounge)
    world.spawner.procs[0].dead = True
    assert world.sleep.check() is True and len(world.spawner.procs) == 2 and len(world.spawner.running) == 1


def test_a_room_that_goes_offline_while_playing_ours_releases_the_assertion(world):
    s = world.system
    play_url(s.lounge, OURS)
    poll(world, s.lounge)
    s.devices.remove(s.lounge)
    world.clock.advance(60)
    world.monitor.tick()
    assert world.sleep.check() is False


def test_stop_releases_and_the_thread_starts_and_stops(world):
    play_url(world.system.lounge, OURS)
    poll(world, world.system.lounge)
    world.sleep.check_every = 0.01
    world.sleep.start()
    world.sleep.stop()
    assert world.spawner.procs[0].terminated and world.sleep._thread is None


def test_grace_period_comes_from_the_config(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("pause_grace_minutes = 5\n")
    assert config.load(p).pause_grace_minutes == 5.0
    assert config.load(tmp_path / "none.toml").pause_grace_minutes == 30.0
    p.write_text('pause_grace_minutes = "soon"\n')
    with pytest.raises(config.ConfigError):
        config.load(p)


def test_a_speaker_that_stopped_answering_no_longer_holds_the_assertion(world, monkeypatch):
    from fakes import FakeSoCo
    play_url(world.system.lounge, OURS)
    poll(world, world.system.lounge)
    assert world.sleep.held
    monkeypatch.setattr(FakeSoCo, "get_current_transport_info", lambda self: (_ for _ in ()).throw(OSError("timeout")))
    poll(world, world.system.lounge)                              # last known state still reads PLAYING/ours, but unreachable
    assert world.monitor.state(world.system.lounge.uid)["state"] == "PLAYING"
    assert world.sleep.check() is False


def test_nothing_new_is_started_once_the_manager_is_stopping(world):
    play_url(world.system.lounge, OURS)
    world.sleep._stop.set()
    poll(world, world.system.lounge)
    assert world.spawner.procs == []


def test_a_non_finite_or_absurd_grace_period_is_a_config_error(tmp_path):
    p = tmp_path / "config.toml"
    for bad in ("nan", "inf", "-1", "1e9"):
        p.write_text(f"pause_grace_minutes = {bad}\n")
        with pytest.raises(config.ConfigError):
            config.load(p)
