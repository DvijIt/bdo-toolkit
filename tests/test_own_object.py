"""The shared own-object ID: profile layout, learning, forgetting and conflicts."""
from dataclasses import replace
import json

import pytest

from bdo_toolkit import load_opcode_profile
from bdo_toolkit._own_object import OwnObjectError, OwnObjectTracker
from bdo_toolkit._protocol import FlowKey
from bdo_toolkit.profiles import OwnObjectProfileLayout, OwnObjectSource, ProfileError
from _support.tracked import FLOW, ME, MOUNT, OTHER_PLAYER, OWN_OBJECT, RESOURCE, position, profile_file, resource


def tracker(**kwargs):
    seen = []
    own = OwnObjectTracker(OWN_OBJECT, **kwargs)
    own.subscribe(seen.append)
    return own, seen


def test_layout_roundtrip_and_validation(tmp_path):
    profile = load_opcode_profile(profile_file(tmp_path, own_object=OWN_OBJECT))
    assert profile.own_object == OWN_OBJECT
    assert profile.to_dict()["own_object"] == {
        "sources": [{"opcode": "0x1CB0", "message_length": 46, "object_id_offset": 38, "flag": 0}],
        "encoding": "object-u32le-v1"}
    with pytest.raises(ProfileError):
        replace(profile, own_object={})
    for offset in (43, 4):
        with pytest.raises(ProfileError):
            OwnObjectSource(0x1CB0, 46, offset)
    with pytest.raises(ProfileError, match="distinct"):
        OwnObjectProfileLayout((OwnObjectSource(0x1CB0, 46, 38), OwnObjectSource(0x1CB0, 46, 30)))
    with pytest.raises(ProfileError):
        OwnObjectProfileLayout(())


@pytest.mark.parametrize("change", [{"encoding": "x"}, {"sources": {}}, {"extra": 1},
                                    {"sources": [{"opcode": "0x1CB0", "message_length": 46}]}])
def test_unrecognized_profile_entry_rejected(tmp_path, change):
    path = profile_file(tmp_path, own_object=OWN_OBJECT)
    data = json.loads(path.read_text())
    data["own_object"].update(change)
    path.write_text(json.dumps(data))
    with pytest.raises(ProfileError):
        load_opcode_profile(path)


def test_learns_from_source_only_and_skips_mounts():
    own, seen = tracker()
    assert own.can_identify and own.own_object_id is None
    own.observe(position(ME))  # Sent for every nearby mover: never sets the ID.
    own.observe(resource(MOUNT, current=32765, maximum=33637))
    assert own.own_object_id is None
    own.observe(resource(ME))
    own.observe(resource(ME, at=2))
    assert own.own_object_id == ME and own.is_own(ME) and not own.is_own(OTHER_PLAYER)
    assert seen == [ME]


def test_forgets_on_close_and_new_epoch_then_relearns():
    own, seen = tracker()
    own.observe(resource(ME))
    own.close_flow(FlowKey("192.0.2.9", 8889, "192.0.2.2", 40001))  # Another flow: kept.
    assert own.own_object_id == ME
    own.close_flow(FLOW)
    assert own.own_object_id is None
    own.observe(resource(ME, epoch=2))
    own.observe(position(OTHER_PLAYER, epoch=3))  # Any frame on a new epoch forgets.
    assert own.own_object_id is None
    own.observe(resource(OTHER_PLAYER, epoch=3))  # A relog has a new ID.
    assert seen == [ME, None, ME, None, OTHER_PLAYER]


def test_two_players_on_one_connection_and_shape_change_raise():
    own, _ = tracker()
    own.observe(resource(ME))
    with pytest.raises(OwnObjectError, match="two players"):
        own.observe(resource(OTHER_PLAYER))
    own, _ = tracker()
    with pytest.raises(OwnObjectError, match="shape"):
        own.observe(resource(ME, layout=replace(RESOURCE, message_length=47)))


def test_given_id_is_fixed():
    own, seen = tracker(object_id=OTHER_PLAYER)
    own.observe(resource(ME))
    own.close_flow(FLOW)
    assert own.own_object_id == OTHER_PLAYER and seen == []
    assert OwnObjectTracker(None, object_id=ME).can_identify
    assert not OwnObjectTracker(None).can_identify
    for bad in [True, -1, 1 << 32, "0x1"]:
        with pytest.raises(ValueError):
            OwnObjectTracker(None, object_id=bad)
