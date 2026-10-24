from api import clock
from common import config

CAPS = ["rcs", "whatsapp", "email"]


def device(state="sent", caps=CAPS):
    return {"recipient_id": "live", "state": state, "channel_caps": caps}


def meta(stage=1, stage_at="2026-10-26T12:00:00Z", window=90, status="running"):
    return {"status": status, "stage": stage, "stage_at": stage_at, "window_seconds": window}


def test_next_rung_is_the_devices_own_ladder_and_due_is_stage_time_plus_window():
    step = clock.next_step(device(), meta())
    assert step == {"next_stage": 2, "next_channel": "whatsapp", "verdict": False, "ends_as": None, "sent_here": False,
                    "tried": ["rcs"], "window_seconds": 90, "due_at": "2026-10-26T12:01:30Z"}


def test_a_read_but_unacknowledged_device_still_gets_the_next_rung():
    assert clock.next_step(device("read"), meta())["next_channel"] == "whatsapp"


def test_before_stage_one_is_recorded_there_is_no_due_time():
    step = clock.next_step(device("none"), meta(stage=0, stage_at=None))
    assert step["next_stage"] == 1 and step["next_channel"] == "rcs" and step["tried"] == [] and step["due_at"] is None


def test_after_the_last_rung_the_next_stage_is_the_verdict():
    step = clock.next_step(device("delivered"), meta(stage=3))
    assert step["next_stage"] == 4 and step["next_channel"] is None and step["verdict"] is True and step["tried"] == CAPS


def test_a_device_with_fewer_channels_reaches_its_verdict_earlier():
    step = clock.next_step(device(caps=["rcs"]), meta(stage=1))
    assert step["next_stage"] == 2 and step["verdict"] is True and step["tried"] == ["rcs"]


def test_a_stored_window_beats_the_configured_default(monkeypatch):
    monkeypatch.setattr(config, "WINDOW_SECONDS", 90)
    step = clock.next_step(device(), meta(window=45))
    assert step["window_seconds"] == 45 and step["due_at"] == "2026-10-26T12:00:45Z"


def test_without_a_stored_window_the_configured_default_applies(monkeypatch):
    monkeypatch.setattr(config, "WINDOW_SECONDS", 60)
    m = meta()
    del m["window_seconds"]
    step = clock.next_step(device(), m)
    assert step["window_seconds"] == 60 and step["due_at"] == "2026-10-26T12:01:00Z"


def test_no_clock_when_the_notice_is_not_running_or_the_device_is_finished():
    assert clock.next_step(device(), meta(status="ready")) is None
    assert clock.next_step(device(), meta(status="closed", stage=4)) is None
    assert clock.next_step(device("acknowledged"), meta()) is None
    assert clock.next_step(device("unreachable"), meta(stage=3)) is None


def test_after_the_last_rung_the_card_names_where_the_device_ends():
    assert clock.next_step(device("delivered"), meta(stage=3))["ends_as"] == "delivered"
    assert clock.next_step(device("read"), meta(stage=3))["ends_as"] == "read"
    assert clock.next_step(device("sent"), meta(stage=3))["ends_as"] == "unreachable"  # never delivered: the engine's verdict
    assert clock.next_step(device("none", caps=[]), meta(stage=1))["ends_as"] == "unreachable"


def test_the_clock_moves_on_once_this_devices_own_send_is_recorded_before_the_stage_is():
    behind = {**device("sent"), "stage": 1}
    ahead = {**device("sent"), "stage": 2}  # stage 2 reached this device while meta.stage still says 1
    assert clock.next_step(behind, meta())["sent_here"] is False
    assert clock.next_step(ahead, meta())["sent_here"] is True
