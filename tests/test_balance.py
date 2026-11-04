"""Left/right balance for each room: the mapping, which rooms have it, the API and the CLI (docs/quick/07)."""

import pytest

from fakes import FakeSystem
from sonos_cli import balance
from test_play_api import post_json, setup_api
from test_rooms_api import running  # noqa: F401  (fixture)
from test_cli import run_cli


# ---- the mapping ----------------------------------------------------------------------------------------------------

def test_every_balance_maps_to_channels_and_back_exactly():
    for b in range(-100, 101):
        left, right = balance.to_channels(b)
        assert 0 <= left <= 100 and 0 <= right <= 100 and 100 in (left, right)
        assert balance.from_channels(left, right) == b
    assert balance.to_channels(0) == (100, 100)
    assert balance.to_channels(62) == (38, 100) and balance.from_channels(38, 100) == 62
    assert balance.to_channels(-80) == (100, 20) and balance.from_channels(100, 20) == -80
    assert balance.from_channels(50, 70) == 20 and balance.from_channels(90, 40) == -50      # neither side at 100


# ---- which rooms have it --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("model", ["Sonos Five", "Sonos Port", "Sonos Amp", "Sonos Play:5", "Sonos PLAY:3", "Sonos Connect",
                                   "Sonos CONNECT:AMP", "connect:amp"])
def test_stereo_products_have_balance(model):
    assert balance.model_has_balance(model)
    assert balance.room_has_balance({"name": "X", "model": model, "members": [{"name": "X", "visible": True}]})


@pytest.mark.parametrize("model", ["Sonos Play:1", "Sonos One", "Sonos One SL", "Sonos Era 100", "", None])
def test_a_single_mono_speaker_has_no_balance(model):
    assert not balance.room_has_balance({"name": "X", "model": model, "members": [{"name": "X", "visible": True}]})


def room(uid, model, *members):
    return {"uid": uid, "name": "X", "model": model, "members": [{"uid": u, "name": "X", "visible": v, "channel": c} for u, v, c in members]}


def test_each_half_of_a_stereo_pair_has_balance():
    assert balance.room_has_balance(room("A", "Sonos Play:1", ("A", True, "LF"), ("B", False, "RF")))
    assert balance.room_has_balance(room("B", "Sonos Play:1", ("A", False, "LF"), ("B", True, "RF")))       # the right half leads


@pytest.mark.parametrize("own, others", [
    ("LF,RF", [("S", False, "SW")]),                                   # a Play:1 with a bonded Sub
    ("LF,RF", [("L", False, "LR"), ("R", False, "RR")]),               # surrounds
    ("LF,RF", [("S", False, "SW"), ("L", False, "LR"), ("R", False, "RR")]),
    (None, [("P", True, "LF"), ("Q", False, "RF")]),                    # a mono room grouped with someone else's pair
])
def test_a_sub_surrounds_or_another_rooms_pair_does_not_give_a_mono_speaker_balance(own, others):
    assert not balance.room_has_balance(room("A", "Sonos Play:1", ("A", True, own), *others))


def test_a_renamed_pair_keeps_its_balance(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    assert post_json(client, f"/api/rooms/{system.office.uid}/rename", {"name": "Studio"}).status_code == 200
    rooms = {r["name"]: r for r in client.get("/api/rooms").json()}
    assert rooms["Studio"]["balance_supported"] is True              # the satellite still has the old name here
    assert post_json(client, f"/api/rooms/{system.office.uid}/balance", {"balance": 20}).status_code == 200


def test_the_room_list_says_which_rooms_have_balance(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    got = {r["name"]: r["balance_supported"] for r in client.get("/api/rooms").json()}
    assert got == {"Office": True, "Kitchen": True, "Lounge": True, "Bedroom": False}


# ---- the API --------------------------------------------------------------------------------------------------------

def test_reading_and_setting_balance_on_each_room_that_has_it(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    for dev in (system.office, system.kitchen, system.lounge):
        assert client.get(f"/api/rooms/{dev.uid}/balance").json() == {"balance": 0, "left": 100, "right": 100}
    for b in range(-100, 101):
        r = post_json(client, f"/api/rooms/{system.lounge.uid}/balance", {"balance": b})
        assert r.status_code == 200 and r.json()["balance"] == b, b
        assert system.lounge.balance == balance.to_channels(b)
    r = post_json(client, f"/api/rooms/{system.office.uid}/balance", {"balance": 62})
    assert r.json() == {"balance": 62, "left": 38, "right": 100}
    assert system.office.balance == (38, 100) and system.office_satellite.balance == (38, 100)        # the pair's other half follows
    assert client.get(f"/api/rooms/{system.office.uid}/balance").json()["balance"] == 62


def test_balance_is_the_rooms_own_not_its_groups_and_leaves_the_volume_alone(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    post_json(client, f"/api/rooms/{system.kitchen.uid}/group", {"into": system.lounge.uid})
    vols = {d.uid: d.volume for d in system.devices}
    assert post_json(client, f"/api/rooms/{system.kitchen.uid}/balance", {"balance": -30}).status_code == 200
    assert system.kitchen.balance == (100, 70) and system.lounge.balance == (100, 100)
    assert {d.uid: d.volume for d in system.devices} == vols


@pytest.mark.parametrize("body", [{"balance": 1.5}, {"balance": True}, {"balance": "10"}, {"balance": None}, {}, {"balance": -101},
                                  {"balance": 101}, {"balance": 10.0}])
def test_a_bad_balance_is_refused_400_and_the_speaker_is_unchanged(tmp_path, body):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    r = post_json(client, f"/api/rooms/{system.lounge.uid}/balance", body)
    assert r.status_code == 400 and "balance" in r.json()["error"]
    assert system.lounge.balance == (100, 100) and not any(c[0] == "balance" for c in system.lounge.calls)
    assert post_json(client, f"/api/rooms/{system.lounge.uid}/balance", {"balance": 10}).status_code == 200      # control


def test_unknown_room_mono_room_and_failing_speaker(tmp_path):
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    assert post_json(client, "/api/rooms/RINCON_NOPE/balance", {"balance": 10}).status_code == 404
    assert client.get("/api/rooms/RINCON_NOPE/balance").status_code == 404
    r = post_json(client, f"/api/rooms/{system.bedroom.uid}/balance", {"balance": 10})
    assert r.status_code == 422 and "Bedroom" in r.json()["error"] and "mono" in r.json()["error"]
    assert client.get(f"/api/rooms/{system.bedroom.uid}/balance").status_code == 422
    assert not any(c[0] == "balance" for c in system.bedroom.calls)
    system.kitchen.fail_balance = True
    r = post_json(client, f"/api/rooms/{system.kitchen.uid}/balance", {"balance": 10})
    assert r.status_code == 502 and "balance" in r.json()["error"]
    assert client.get(f"/api/rooms/{system.kitchen.uid}/balance").status_code == 502
    assert post_json(client, f"/api/rooms/{system.lounge.uid}/balance", {"balance": 10}).status_code == 200    # control


# ---- the CLI --------------------------------------------------------------------------------------------------------

def test_cli_sets_and_reads_balance_through_the_service(running, capsys):
    s = running.system
    assert run_cli("-r", "Lounge", "balance", "30") == 0 and s.lounge.balance == (70, 100)
    assert run_cli("-r", "lounge", "balance", "-20") == 0 and s.lounge.balance == (100, 80)
    assert run_cli("-r", "Lounge", "balance") == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["30 (right 30)", "-20 (left 20)", "-20 (left 20)"]
    posts = [(p.split("/")[-1], b) for m, p, b in running.app.calls() if m == "POST"]
    assert posts == [("balance", {"balance": 30}), ("balance", {"balance": -20})]


def test_cli_refuses_a_mono_room_and_an_out_of_range_value(running, capsys):
    assert run_cli("-r", "Bedroom", "balance", "10") == 1
    assert "mono" in capsys.readouterr().err
    sent = len([c for c in running.app.calls() if c[0] == "POST"])
    assert run_cli("-r", "Lounge", "balance", "150") == 1
    assert "-100" in capsys.readouterr().err
    assert len([c for c in running.app.calls() if c[0] == "POST"]) == sent         # refused before anything was sent
    assert running.system.lounge.balance == (100, 100)


def test_cli_balance_works_without_the_service(monkeypatch, capsys):
    system = FakeSystem().install(monkeypatch)
    assert run_cli("-r", "Kitchen", "balance", "-40") == 0 and system.kitchen.balance == (100, 60)
    assert capsys.readouterr().out.strip() == "-40 (left 40)"


def test_cli_without_the_service_picks_the_room_by_ip_and_refuses_a_mono_room(monkeypatch, capsys):
    system = FakeSystem().install(monkeypatch)
    assert run_cli("--ip", "192.168.0.29", "balance", "30") == 0                       # Kitchen's speaker
    assert system.kitchen.balance == (70, 100) and system.office.balance == (100, 100)
    monkeypatch.setenv("SONOS_IP", "192.168.0.26")
    assert run_cli("balance", "-10") == 0 and system.lounge.balance == (100, 90)        # $SONOS_IP, as for the other commands
    monkeypatch.delenv("SONOS_IP")
    assert run_cli("-r", "Kitchen", "--ip", "192.168.0.26", "balance", "5") == 1        # an IP that isn't that room's
    assert "belongs to Lounge" in capsys.readouterr().err
    assert run_cli("-r", "Bedroom", "balance", "30") == 1
    assert "mono" in capsys.readouterr().err and system.bedroom.balance == (100, 100)


def test_two_overlapping_changes_never_leave_a_mix_of_both(tmp_path, monkeypatch):
    """A real speaker takes left and right as two separate calls; two changes at once must not interleave them."""
    import threading
    import time
    system, music, cfg, lib, monitor, client = setup_api(tmp_path)
    dev = system.lounge

    def two_calls(left_right):
        left, right = left_right
        dev._channels = (left, dev._channels[1])            # LF first...
        time.sleep(0.05)                                    # ...the other request gets in here without a lock
        dev._channels = (dev._channels[0], right)           # ...then RF
    monkeypatch.setattr(type(dev), "balance", property(lambda self: self._channels, lambda self, v: two_calls(v)))
    results = []
    for _ in range(5):
        ts = [threading.Thread(target=lambda b=b: results.append(post_json(client, f"/api/rooms/{dev.uid}/balance", {"balance": b}).status_code))
              for b in (-20, 20)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert dev._channels in ((100, 80), (80, 100)), dev._channels          # one whole value or the other, never (80, 80)
    assert results == [200] * 10
