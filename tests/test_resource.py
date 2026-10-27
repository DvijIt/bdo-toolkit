"""Class resource changes: optional profile layout, decoding, own marking via the shared own object, replay."""
from dataclasses import replace
import json

import pytest

import bdo_toolkit
from bdo_toolkit import load_opcode_profile
from bdo_toolkit._own_object import OwnObjectTracker
from bdo_toolkit.resource import LiveResourceSession, ResourceTrackingError, replay_resource
from bdo_toolkit.resource._tracker import ResourceTracker
from bdo_toolkit.profiles import ProfileError, ResourceProfileLayout
from _support.tracked import ME, MOUNT, OTHER_PLAYER, OWN_OBJECT, RESOURCE, profile_file, resource, write_capture


def tracker(**kwargs):
    readings = []
    return ResourceTracker(RESOURCE, readings.append, OwnObjectTracker(OWN_OBJECT), **kwargs), readings


def test_layout_roundtrip(tmp_path):
    profile = load_opcode_profile(profile_file(tmp_path, resource=RESOURCE))
    assert profile.resource == RESOURCE
    assert profile.to_dict()["resource"] == {"opcode": "0x1CB0", "message_length": 46, "maximum_offset": 30,
                                       "change_offset": 34, "object_id_offset": 38, "current_offset": 42,
                                       "flag": 0, "encoding": "resource-u32le-change-i32le-v1"}
    assert "resource" not in replace(profile, resource=None).to_dict()


@pytest.mark.parametrize("change", [{"current_offset": 43}, {"change_offset": 32}, {"flag": 1},
                                    {"encoding": "x"}, {"extra": 1}, {"opcode": "nope"}])
def test_invalid_layout_rejected(tmp_path, change):
    path = profile_file(tmp_path, resource=RESOURCE)
    data = json.loads(path.read_text())
    data["resource"].update(change)
    path.write_text(json.dumps(data))
    with pytest.raises(ProfileError):
        load_opcode_profile(path)


def test_decodes_and_marks_own_in_the_same_message():
    t, readings = tracker()
    t.observe(resource(MOUNT, current=32765, change=-5, maximum=33637))
    t.observe(resource(ME, current=1754, change=-90))
    t.observe(resource(ME, current=1840, change=+375, at=2))
    assert [(r.object_id, r.change, r.current, r.is_own) for r in readings] == [
        (MOUNT, -5, 32765, False), (ME, -90, 1754, True), (ME, 375, 1840, True)]
    assert readings[1].percentage == pytest.approx(95.326, abs=1e-3)
    status = t.snapshot()
    assert status.status == "tracking" and status.reading.current == 1840 and status.own_object_id == ME
    assert readings[0].to_dict()["object_id"] == "0x20086406"


def test_own_only_drops_the_mount_and_relog_forgets():
    t, readings = tracker(own_only=True)
    t.observe(resource(MOUNT, current=32765, maximum=33637))
    t.observe(resource(ME))
    assert [r.object_id for r in readings] == [ME]
    t.observe(resource(MOUNT, current=32700, maximum=33637, epoch=2))
    assert t.snapshot().status == "waiting" and t.snapshot().own_object_id is None


@pytest.mark.parametrize("bad", [dict(current=1841), dict(maximum=0, current=0),
                                 dict(layout=replace(RESOURCE, message_length=47)), dict(at=float("nan"))])
def test_bad_values_or_shape_withdraw_authority(bad):
    t, _ = tracker()
    with pytest.raises(ResourceTrackingError):
        t.observe(resource(**bad))
    assert t.snapshot().status == "invalid"


def test_two_players_withdraw_authority():
    t, _ = tracker()
    t.observe(resource(ME))
    with pytest.raises(ResourceTrackingError, match="two players"):
        t.observe(resource(OTHER_PLAYER))


def test_replay(tmp_path):
    path = tmp_path / "capture.pcap"
    write_capture(path, [resource(MOUNT, current=32765, maximum=33637), resource(ME, at=2), resource(ME, 1840, 86, at=3)])
    profile = load_opcode_profile(profile_file(tmp_path, resource=RESOURCE, own_object=OWN_OBJECT))
    with replay_resource(path, profile=profile) as replay:
        assert [(r.object_id, r.is_own) for r in replay] == [(MOUNT, False), (ME, True), (ME, True)]
        assert replay.status.reading.current == 1840


def test_missing_layout_rejected(tmp_path):
    with pytest.raises(ProfileError, match="no resource layout"):
        LiveResourceSession(profile=load_opcode_profile(profile_file(tmp_path)))
    with pytest.raises(ProfileError, match="own_only"):
        LiveResourceSession(profile=load_opcode_profile(profile_file(tmp_path, resource=RESOURCE)), own_only=True)
    assert isinstance(RESOURCE, ResourceProfileLayout)


def test_public_export():
    assert bdo_toolkit.resource.replay_resource is replay_resource
    assert "resource" in bdo_toolkit.__all__
