"""World position: optional profile layout, per-object decoding, own marking, replay and live ownership."""
from dataclasses import replace
import json

import pytest

import bdo_toolkit
from bdo_toolkit import CaptureIntegrityError, LiveCaptureOptions, load_opcode_profile
from bdo_toolkit import _tracked_capture as capture_module
from bdo_toolkit._own_object import OwnObjectTracker
from bdo_toolkit._profile_runtime import validate_runtime_profile
from bdo_toolkit.calibration import MessageSpec, update_profile
from bdo_toolkit.position import LivePositionSession, PositionTrackingError, replay_position
from bdo_toolkit.position._tracker import PositionTracker
from bdo_toolkit.profiles import PositionProfileLayout, ProfileError
from _support.tracked import (ME, MOVE, MOVE_FACING, MOUNT, OTHER_PLAYER, OWN_OBJECT, POSITION, POSITION_FACING,
                              UPDATE, position, profile_file, resource,
                              write_capture)
from test_agris_session import FakeCapture, wait_stopped


def tracker(*, own_layout=OWN_OBJECT, object_id=None, own_only=False):
    readings = []
    own = OwnObjectTracker(own_layout, object_id=object_id)
    return PositionTracker(POSITION, readings.append, own, own_only=own_only), readings


def test_layout_roundtrip_is_optional_and_immutable(tmp_path):
    profile = load_opcode_profile(profile_file(tmp_path, position=POSITION))
    assert profile.position == POSITION
    assert profile.to_dict()["position"] == {
        "sources": [{"opcode": "0x0C3F", "message_length": 46, "position_offset": 9, "object_id_offset": 21,
                     "facing_offset": None, "flag": 0}],
        "encoding": "xyz-f32le-object-u32le-v2"}
    validate_runtime_profile(profile)
    with pytest.raises(AttributeError):
        profile.position.sources[0].position_offset = 1
    without = replace(profile, position=None)
    assert "position" not in without.to_dict()
    validate_runtime_profile(without)
    with pytest.raises(ProfileError):
        replace(profile, position={})


@pytest.mark.parametrize("change", [
    {"opcode": True}, {"opcode": 65536}, {"message_length": 20}, {"message_length": 4097},
    {"position_offset": 4}, {"position_offset": 35}, {"object_id_offset": 43}, {"object_id_offset": 17},
    {"flag": 1}, {"flag": False}, {"facing_offset": 18}, {"heading_offset": 30},
])
def test_invalid_or_unrecognized_layout_rejected(tmp_path, change):
    path = profile_file(tmp_path, position=POSITION)
    data = json.loads(path.read_text())
    data["position"]["sources"][0].update(change)
    path.write_text(json.dumps(data))
    with pytest.raises(ProfileError):
        load_opcode_profile(path)


def test_item_update_preserves_position_and_own_object(tmp_path):
    path = profile_file(tmp_path, position=POSITION, own_object=OWN_OBJECT)
    update_profile([MessageSpec("STORAGE_ITEM_DELTA", 0x1000, 40,
                                item_id_offset=10, quantity_added_offset=14)], path)
    profile = load_opcode_profile(path)
    assert (profile.position, profile.own_object) == (POSITION, OWN_OBJECT)


def test_every_nearby_object_per_object_repeats():
    t, readings = tracker()
    for f in [position(ME), position(OTHER_PLAYER, 1.0), position(ME, at=2), position(OTHER_PLAYER, 1.0, at=3),
              position(ME, 40510.5, -919.25, -137859.5, at=4)]:
        t.observe(f)
    assert [(r.object_id, r.x) for r in readings] == [(ME, 40429.0), (OTHER_PLAYER, 1.0), (ME, 40510.5)]
    assert not any(r.is_own for r in readings)
    assert t.snapshot().status == "waiting" and t.snapshot().own_object_id is None
    assert readings[0].to_dict()["object_id"] == "0x00E864AC"
    with pytest.raises(AttributeError):
        readings[0].x = 0


def test_resource_reveals_own_and_reports_last_position_as_own():
    t, readings = tracker()
    t.observe(position(ME))
    t.observe(position(OTHER_PLAYER, 1.0))
    t.observe(resource(MOUNT, current=32765, maximum=33637))  # The ridden mount is no player.
    assert t.snapshot().own_object_id is None
    t.observe(resource(ME, at=2))
    assert readings[-1].object_id == ME and readings[-1].is_own and readings[-1].x == 40429.0
    t.observe(position(OTHER_PLAYER, 2.0, at=3))
    t.observe(position(ME, 5.0, at=4))
    status = t.snapshot()
    assert status.status == "tracking" and status.own_object_id == ME
    assert (status.reading.x, status.reading.is_own) == (5.0, True)
    assert status.to_dict()["own_object_id"] == "0x00E864AC"


def test_own_only_and_reconnect_forgets_own():
    t, readings = tracker(own_only=True)
    t.observe(position(OTHER_PLAYER, 1.0))
    t.observe(position(ME))
    assert readings == []
    t.observe(resource(ME, at=2))
    t.observe(position(OTHER_PLAYER, 2.0, at=3))
    t.observe(position(ME, 6.0, at=4))
    assert [r.x for r in readings] == [40429.0, 6.0]
    t.observe(position(ME, 7.0, at=5, epoch=2))  # Relog: the ID is new and unknown.
    assert t.snapshot().status == "waiting" and t.snapshot().own_object_id is None
    assert [r.x for r in readings] == [40429.0, 6.0]


def test_given_object_id_and_own_only_needs_a_source():
    t, readings = tracker(own_layout=None, object_id=ME, own_only=True)
    t.observe(position(OTHER_PLAYER, 1.0))
    t.observe(position(ME))
    assert [r.object_id for r in readings] == [ME] and readings[0].is_own
    with pytest.raises(ProfileError, match="own_only"):
        tracker(own_layout=None, own_only=True)


@pytest.mark.parametrize("bad", [
    dict(source=replace(MOVE, message_length=47)), dict(flag=1), dict(x=float("nan")),
    dict(y=float("inf")), dict(z=-2e7), dict(at=float("nan")),
])
def test_wrong_shape_or_values_withdraw_authority(bad):
    t, _ = tracker()
    t.observe(position())
    with pytest.raises(PositionTrackingError):
        t.observe(position(**bad))
    assert t.snapshot().status == "invalid" and t.snapshot().reading is None
    with pytest.raises(PositionTrackingError):
        t.observe(position())


def test_own_object_conflict_withdraws_authority():
    t, _ = tracker()
    t.observe(resource(ME))
    with pytest.raises(PositionTrackingError, match="two players"):
        t.observe(resource(OTHER_PLAYER))


def test_replay_learns_own_from_capture(tmp_path):
    path = tmp_path / "capture.pcap"
    write_capture(path, [position(OTHER_PLAYER, 1.0), position(ME, at=2), resource(ME, at=3),
                         position(ME, 2.0, at=4), position(OTHER_PLAYER, 3.0, at=5)])
    profile = load_opcode_profile(profile_file(tmp_path, position=POSITION, own_object=OWN_OBJECT))
    with replay_position(path, profile=profile, own_only=True) as replay:
        assert [(r.x, r.is_own) for r in replay] == [(40429.0, True), (2.0, True)]
        assert replay.status.own_object_id == ME and replay.status.reading.x == 2.0
        assert replay.health.packets_processed == 5


def test_replay_error_hides_partial_state(tmp_path):
    path = tmp_path / "capture.pcap"
    write_capture(path, [position(), position(ME, float("nan"), at=2)])
    with replay_position(path, profile=load_opcode_profile(profile_file(tmp_path, position=POSITION))) as replay:
        with pytest.raises(PositionTrackingError):
            list(replay)
        assert replay.status.status == "invalid" and replay.status.reading is None


@pytest.fixture
def fake(monkeypatch):
    captures = []

    class Capture(FakeCapture):
        frames = [position(), resource(ME, at=2), position(ME, 1.0, at=3)]

        def start(self):
            self.running = True
            for f in self.frames:
                try:
                    self.callback(f)
                except BaseException as exc:
                    self.error = exc
                    break

    def factory(**kwargs):
        capture = Capture(**kwargs)
        captures.append(capture)
        return capture
    monkeypatch.setattr(capture_module, "LivePacketCapture", factory)
    monkeypatch.setattr(capture_module, "make_packet_handler", lambda manager: manager.observe)
    return Capture, captures


def test_live_session(tmp_path, fake):
    profile = load_opcode_profile(profile_file(tmp_path, position=POSITION, own_object=OWN_OBJECT))
    owner = LivePositionSession(profile=profile, capture_seconds=.02)
    assert owner.status.status == "waiting"
    with owner:
        wait_stopped(owner)
        events = list(owner.events())
    assert [(r.x, r.is_own) for r in events] == [(40429.0, False), (40429.0, True), (1.0, True)]
    assert owner.status.reading.x == 1.0 and owner.status.own_object_id == ME
    assert owner.health.packets_processed == 3
    assert owner.stop_reason == "duration"


def test_missing_or_inactive_profile_rejected_before_acquisition(tmp_path, fake):
    with pytest.raises(ProfileError, match="no position layout"):
        LivePositionSession(profile=load_opcode_profile(profile_file(tmp_path)))
    with pytest.raises(ProfileError):
        LivePositionSession(profile=replace(load_opcode_profile(profile_file(tmp_path, position=POSITION)),
                                            active=False))
    with pytest.raises(ProfileError, match="own_only"):
        LivePositionSession(profile=load_opcode_profile(profile_file(tmp_path, position=POSITION)), own_only=True)
    assert not fake[1]


def test_queue_overflow_fails_closed(tmp_path, fake):
    owner = LivePositionSession(profile=load_opcode_profile(profile_file(tmp_path, position=POSITION)),
                                live_options=LiveCaptureOptions(event_queue_size=1), capture_seconds=.02)
    try:
        owner.start()
    except CaptureIntegrityError:
        pass
    wait_stopped(owner)
    with pytest.raises(CaptureIntegrityError):
        owner.poll(0)
    assert owner.status.status == "invalid" and not owner.health.capture_is_clean


def test_public_export():
    assert bdo_toolkit.position.replay_position is replay_position
    assert "position" in bdo_toolkit.__all__


def facing_position(**kwargs):
    return position(source=MOVE_FACING, **kwargs)


def test_facing_layout_roundtrip_and_overlap(tmp_path):
    profile = load_opcode_profile(profile_file(tmp_path, position=POSITION_FACING))
    assert profile.position == POSITION_FACING
    assert [s["facing_offset"] for s in profile.to_dict()["position"]["sources"]] == [29, 25]
    for offset in (18, 35, 4):  # Over the ID, past the end, into the header.
        with pytest.raises(ProfileError):
            replace(MOVE_FACING, facing_offset=offset)
    with pytest.raises(ProfileError, match="distinct"):
        PositionProfileLayout((MOVE, MOVE_FACING))
    with pytest.raises(ProfileError):
        PositionProfileLayout(())
    for change in ({"encoding": "xyz-f32le-object-u32le-v1"}, {"extra": 1}, {"sources": {}}):
        path = profile_file(tmp_path, position=POSITION)
        data = json.loads(path.read_text())
        data["position"].update(change)
        path.write_text(json.dumps(data))
        with pytest.raises(ProfileError):
            load_opcode_profile(path)


@pytest.mark.parametrize(("facing", "heading"), [
    ((0.0, 0.0, 1.0), 0.0), ((1.0, 0.0, 0.0), 90.0), ((0.0, 0.0, -1.0), 180.0), ((-1.0, 0.0, 0.0), 270.0),
    ((0.9976, 0.0, 0.0698), 86.0),  # Client 3464, standing in the mount toggle capture.
])
def test_heading_is_degrees_clockwise_from_north(facing, heading):
    readings = []
    PositionTracker(POSITION_FACING, readings.append, OwnObjectTracker(None)).observe(facing_position(facing=facing))
    assert readings[0].heading == pytest.approx(heading, abs=0.05)
    assert readings[0].to_dict()["heading"] == pytest.approx(heading, abs=0.05)


def test_turning_on_the_spot_is_a_new_reading_and_no_facing_means_none():
    readings = []
    t = PositionTracker(POSITION_FACING, readings.append, OwnObjectTracker(None))
    t.observe(facing_position())
    t.observe(facing_position(at=2))
    t.observe(facing_position(facing=(1.0, 0.0, 0.0), at=3))
    assert [r.heading for r in readings] == [0.0, 90.0]
    plain, plain_readings = tracker()
    plain.observe(position())
    assert plain_readings[0].heading is None and plain_readings[0].facing_x is None


@pytest.mark.parametrize("facing", [(0.0, 0.0, 0.0), (0.5, 0.0, 0.5), (float("nan"), 0.0, 1.0), (2.0, 0.0, 0.0)])
def test_facing_that_is_no_unit_vector_withdraws_authority(facing):
    t = PositionTracker(POSITION_FACING, lambda reading: None, OwnObjectTracker(None))
    with pytest.raises(PositionTrackingError, match="unit vector"):
        t.observe(facing_position(facing=facing))


def test_latest_message_of_any_source_wins_and_keeps_facing():
    readings = []
    t = PositionTracker(POSITION_FACING, readings.append, OwnObjectTracker(None, object_id=ME), own_only=True)
    t.observe(position(source=MOVE_FACING, facing=(1.0, 0.0, 0.0)))
    # Walking backwards: only the steady update source reports, still facing east.
    for step in range(1, 4):
        t.observe(position(x=40429.0 - 190 * step, source=UPDATE, facing=(1.0, 0.0, 0.0), at=1 + step))
    t.observe(position(x=40000.0, source=MOVE_FACING, facing=(0.0, 0.0, 1.0), at=5))
    assert [(r.x, round(r.heading)) for r in readings] == [
        (40429.0, 90), (40239.0, 90), (40049.0, 90), (39859.0, 90), (40000.0, 0)]
    assert t.snapshot().reading.x == 40000.0


def test_source_without_facing_keeps_the_last_known_facing():
    readings = []
    layout = PositionProfileLayout((MOVE_FACING, replace(UPDATE, facing_offset=None)))
    t = PositionTracker(layout, readings.append, OwnObjectTracker(None))
    t.observe(position(source=UPDATE, x=1.0))  # No facing known yet.
    t.observe(position(source=MOVE_FACING, x=2.0, facing=(1.0, 0.0, 0.0)))
    t.observe(position(source=UPDATE, x=3.0))
    assert [(r.x, r.heading) for r in readings] == [(1.0, None), (2.0, 90.0), (3.0, 90.0)]
