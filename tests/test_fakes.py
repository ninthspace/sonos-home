"""The fake Sonos system, and the live-test helper that is tested against it."""

import pytest
from soco.exceptions import SoCoUPnPException

import live
from fakes import FakeSnapshot, FakeSystem
from sonos_cli import cli, player


@pytest.fixture
def system(monkeypatch):
    return FakeSystem().install(monkeypatch)


def test_five_rooms_with_office_as_coordinator_plus_invisible_satellite(system):
    assert sorted(d.player_name for d in system.devices) == ["Bedroom", "Kitchen", "Lounge", "Office", "Office"]
    assert len(system.devices) == 5
    assert system.office.is_coordinator and system.office.is_visible
    sat = system.office_satellite
    assert not sat.is_visible and sat.coordinator is system.office
    assert system.office.group.members == {system.office, sat}   # members include the satellite
    assert system.discover() == {system.office, system.bedroom, system.kitchen, system.lounge}
    assert system.office.visible_zones == system.discover()


def test_each_room_has_queue_transport_position_volume_mute_group_and_settable_uri(system):
    for d in system.devices:
        assert d.queue_size == 0
        assert d.get_current_transport_info()["current_transport_state"] == "STOPPED"
        assert d.get_current_track_info()["position"] == "0:00:00"
        assert isinstance(d.volume, int) and d.mute is False
        assert d.group.coordinator is d.coordinator
    k = system.kitchen
    k.volume, k.mute = 25, True
    assert (k.volume, k.mute) == (25, True)
    k.set_source("http://other-host:8737/media/x/a.flac")
    assert k.get_current_media_info()["uri"] == "http://other-host:8737/media/x/a.flac"
    k.set_source("x-sonos-vli:abc")
    assert k.source_uri == "x-sonos-vli:abc" and k.get_current_track_info()["uri"] == "x-sonos-vli:abc"


def test_a_group_member_reports_x_rincon_and_follows_the_coordinators_transport(system):
    system.kitchen.join(system.lounge)
    assert system.kitchen.get_current_media_info()["uri"] == f"x-rincon:{system.lounge.uid}"
    assert system.kitchen.get_current_track_info()["uri"] == f"x-rincon:{system.lounge.uid}"
    assert system.kitchen.group.coordinator is system.lounge
    system.lounge.play_uri("http://h/live/a.mp3")
    assert system.kitchen.get_current_transport_info()["current_transport_state"] == "PLAYING"


def test_partymode_joins_every_visible_room_and_the_satellite_moves_with_its_partner(system):
    system.lounge.partymode()
    assert {d for d in system.devices if d.coordinator is system.lounge} == set(system.devices)
    assert system.office_satellite.coordinator is system.lounge          # follows Office into the party
    assert all(call[0] != "join" for call in system.office_satellite.calls)   # and was never told to join
    system.lounge.group.volume = 5
    assert system.office_satellite.volume == 5


def test_join_then_unjoin_takes_the_satellite_back_with_its_partner(system):
    system.office.join(system.lounge)
    assert system.office_satellite.coordinator is system.lounge
    system.office.unjoin()
    assert system.office.group.members == {system.office, system.office_satellite}
    assert system.lounge.group.members == {system.lounge}


def test_a_coordinator_that_leaves_regroups_its_followers(system):
    system.kitchen.join(system.lounge)
    system.lounge.unjoin()
    assert system.lounge.group.members == {system.lounge}
    assert system.kitchen.is_coordinator and system.kitchen.group.members == {system.kitchen}
    system.bedroom.join(system.lounge)
    system.kitchen.join(system.lounge)
    system.lounge.join(system.office)
    assert system.lounge.coordinator is system.office
    assert {d.coordinator for d in (system.bedroom, system.kitchen)} <= {system.bedroom, system.kitchen}
    assert all(d.coordinator.is_coordinator for d in system.devices)     # nobody follows a non-coordinator


def test_transport_and_queue_edits_on_a_group_member_raise_like_soco_and_queues_are_per_device(system):
    from soco.exceptions import SoCoSlaveException
    system.kitchen.join(system.lounge)
    item = player.queue_item(_track(), "http://h/a.flac", 0)
    for call in (system.kitchen.play, system.kitchen.pause, system.kitchen.stop, system.kitchen.next,
                 system.kitchen.previous, lambda: system.kitchen.play_from_queue(0),
                 lambda: system.kitchen.play_uri("http://h/x.mp3"), lambda: system.kitchen.seek("00:00:05"),
                 lambda: system.kitchen.add_to_queue(item), system.kitchen.clear_queue):
        with pytest.raises(SoCoSlaveException):
            call()
    system.kitchen.add_multiple_to_queue([item])             # not coordinator-only in SoCo; it is Kitchen's own queue
    assert system.kitchen.queue_size == 1 and system.lounge.queue_size == 0


def test_leaving_a_group_forgets_the_old_source_and_volume_is_clamped(system):
    system.kitchen.add_to_queue(player.queue_item(_track(), "http://h/a.flac", 0))
    system.kitchen.play_from_queue(0)
    system.kitchen.join(system.lounge)
    system.kitchen.unjoin()
    assert system.kitchen.source_uri is None
    system.kitchen.volume = 150
    assert system.kitchen.volume == 100
    system.kitchen.volume = -5
    assert system.kitchen.volume == 0


def test_seek_by_position_and_by_track_like_soco(system):
    item = player.queue_item(_track(), "http://h/a.flac", 0)
    system.office.add_multiple_to_queue([item, item, item])
    system.office.play_from_queue(0)
    system.office.seek(track=2)
    assert system.office.get_current_track_info()["playlist_position"] == "3"
    system.office.seek(position="0:01:30")
    assert system.office.get_current_track_info()["position"] == "0:01:30"
    for bad in ("1:30", "1:2:3", "-0:00:05"):
        with pytest.raises(ValueError, match="HH:MM:SS"):
            system.office.seek(bad)
    with pytest.raises(ValueError, match="No position or track"):
        system.office.seek()


def test_the_fake_works_wherever_soco_does_player_and_cli(system, capsys):
    assert player.find_room("office").ip_address == "192.168.0.21"
    assert player.find_room("x", ip="192.168.0.31") is system.bedroom
    assert [d.player_name for d in player.list_rooms()] == ["Bedroom", "Kitchen", "Lounge", "Office"]
    item = player.queue_item(_track(), "http://h/media/a.flac", 0)
    system.office.add_multiple_to_queue([item])
    system.office.play_from_queue(0)
    assert system.office.get_current_track_info()["uri"] == "http://h/media/a.flac"
    assert cli.main(["--ip", "192.168.0.21", "pause"]) == 0
    assert system.office.transport_state == "PAUSED_PLAYBACK"
    assert cli.main(["--ip", "192.168.0.21", "volume", "40"]) == 0
    assert system.office.group.volume == 40 and system.office_satellite.volume == 40
    assert cli.main(["--ip", "192.168.0.21", "status"]) == 0
    assert "PAUSED" in capsys.readouterr().out.upper()


def _track():
    from pathlib import Path
    from sonos_cli.media import AudioInfo, Track
    return Track(Path("/music/a.flac"), AudioInfo("flac", 44100, 2, 16, 12.0, "A", None, None))


# ---- live-test helper (FR18) ---------------------------------------------------------------------

def snapshot_failing_402(device, snapshot_queue=False):
    return FakeSnapshot(device, snapshot_queue, restore_error="402")


def office_with_odd_levels(system):
    system.office.volume, system.office.mute = 76, False
    system.office_satellite.volume, system.office_satellite.mute = 70, True
    return system.office


def check_restored(system, reports):
    assert (system.office.volume, system.office.mute) == (76, False)
    assert (system.office_satellite.volume, system.office_satellite.mute) == (70, True)
    assert any("source not restored" in r for r in reports)


def test_restore_failure_402_still_restores_every_members_volume_and_mute(system):
    office = office_with_odd_levels(system)
    reports = []
    with live.muted_room(office, snapshot_cls=snapshot_failing_402, report=reports.append) as members:
        assert {m.ip_address for m in members} == {"192.168.0.21", "192.168.0.27"}
        assert all(m.mute for m in members)           # muted for the test, satellite included
        assert system.office.volume == 76              # muting doesn't touch volume
    check_restored(system, reports)


def test_without_the_separate_volume_and_mute_step_the_same_check_fails(system, monkeypatch):
    def naive_restore_all(coordinator, members, saved, snapshot, report):
        try:
            snapshot.restore(fade=False)               # raises 402 ...
            live.restore_volume_and_mute(members, saved, report)   # ... so this never runs
        except Exception as e:
            report(f"source not restored: {e}")

    monkeypatch.setattr(live, "restore_all", naive_restore_all)
    office = office_with_odd_levels(system)
    reports = []
    with live.muted_room(office, snapshot_cls=snapshot_failing_402, report=reports.append):
        pass
    with pytest.raises(AssertionError):
        check_restored(system, reports)


def test_helper_restores_normally_when_the_source_restores(system):
    office = office_with_odd_levels(system)
    reports = []
    with live.muted_room(office, snapshot_cls=FakeSnapshot, report=reports.append):
        pass
    assert (system.office.volume, system.office.mute) == (76, False)
    assert reports == []


def test_helper_restores_when_the_body_raises(system):
    office = office_with_odd_levels(system)
    with pytest.raises(RuntimeError):
        with live.muted_room(office, snapshot_cls=snapshot_failing_402, report=lambda m: None):
            raise RuntimeError("boom")
    assert (system.office_satellite.volume, system.office_satellite.mute) == (70, True)


def test_playback_is_stopped_before_anything_is_unmuted(system):
    office = office_with_odd_levels(system)
    with live.muted_room(office, snapshot_cls=FakeSnapshot, report=lambda m: None):
        office.play_uri("http://h/live/tone.mp3")
        office.calls.clear()
    names = [c[0] for c in office.calls]
    assert names.index("stop") < names.index("set_mute")
    assert ("set_mute", False) in office.calls


def test_a_failing_member_does_not_stop_the_others_being_restored(system):
    from fakes import FakeSoCo

    class RefusesUnmute(FakeSoCo):
        @property
        def mute(self):
            return self.__dict__.get("_m", False)

        @mute.setter
        def mute(self, value):
            if value is False:
                raise SoCoUPnPException("fake: unmute refused", "500", "")
            self.__dict__["_m"] = value

    office = office_with_odd_levels(system)
    sat = system.office_satellite
    for d in (office, sat):                      # whichever member is restored first fails; the other must still be
        d.__class__ = RefusesUnmute
        d.__dict__["_m"] = False
    office.volume = 61
    reports = []
    with live.muted_room(office, snapshot_cls=FakeSnapshot, report=reports.append):
        office.volume = 10
        sat.volume = 11
    assert sum("mute not restored" in r for r in reports) == 2
    assert office.volume == 61 and sat.volume == 70            # volumes still came back after each failed unmute


def test_a_stop_that_fails_is_reported_not_swallowed(system):
    office = office_with_odd_levels(system)
    reports = []
    system.office.stop = lambda: (_ for _ in ()).throw(SoCoUPnPException("fake: cannot stop", "500", ""))
    with live.muted_room(office, snapshot_cls=FakeSnapshot, report=reports.append):
        pass
    assert any("playback not stopped" in r for r in reports)
    assert (system.office.volume, system.office.mute) == (76, False)
