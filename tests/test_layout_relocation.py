"""Re-finding moved position, own-object and resource opcodes and saving them to a profile."""
from dataclasses import replace
import hashlib
import json

import pytest

from bdo_toolkit import load_opcode_profile
from bdo_toolkit.position import LayoutRelocationError, relocate_position_replay, update_position_profile
from bdo_toolkit.profiles import OwnObjectProfileLayout, OwnObjectSource, PositionProfileLayout, ProfileError
from bdo_toolkit.resource import relocate_resource_replay, update_resource_profile
from _support.tracked import (ME, MOUNT, MOVE, MOVE_FACING, OTHER_PLAYER, OWN_OBJECT, POSITION_FACING, RESOURCE, UPDATE,
                              frame, message, position, profile_file, resource, write_capture)

MOVED_MOVE = replace(MOVE_FACING, opcode=0x2001)
MOVED_UPDATE = replace(UPDATE, opcode=0x2002)
MOVED_RESOURCE = replace(RESOURCE, opcode=0x2003)


def walking(count: int = 12):
    """Two players walking: both position sources under moved opcodes, plus resource messages as 46-byte decoys."""
    frames = []
    for n in range(count):
        at = 1.0 + n
        frames.append(position(ME, 40429.0 + 50 * n, source=MOVED_MOVE, facing=(0.6, 0.0, 0.8), at=at))
        frames.append(position(OTHER_PLAYER, 41000.0 - 30 * n, source=MOVED_UPDATE, facing=(-1.0, 0.0, 0.0), at=at))
        frames.append(resource(ME, current=1754 - n, change=-1, layout=MOVED_RESOURCE, at=at))
    return frames


def capture(tmp_path, frames, name="capture.pcap"):
    path = tmp_path / name
    write_capture(path, frames)
    return path


def profile(tmp_path, **layouts):
    defaults = dict(position=POSITION_FACING, own_object=OWN_OBJECT, resource=RESOURCE)
    return load_opcode_profile(profile_file(tmp_path, **{**defaults, **layouts}))


def test_position_sources_are_found_and_saved(tmp_path):
    result = relocate_position_replay(capture(tmp_path, walking()), profile=profile(tmp_path))
    assert result.layout == PositionProfileLayout((MOVED_MOVE, MOVED_UPDATE)) and result.changed
    assert result.readings == (12, 12)
    path = tmp_path / "profile.json"
    update = update_position_profile(result, path,
                                     expected_profile_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    assert update.written and update.backup_path.exists()
    saved = load_opcode_profile(path)
    assert saved.position == result.layout and saved.resource == RESOURCE
    assert not update_position_profile(result, path).written


def test_resource_and_its_own_object_source_move_together(tmp_path):
    result = relocate_resource_replay(capture(tmp_path, walking()), profile=profile(tmp_path))
    assert result.layout == MOVED_RESOURCE and result.readings == 12
    assert result.own_object == OwnObjectProfileLayout((OwnObjectSource(0x2003, 46, 38),))
    path = tmp_path / "profile.json"
    assert update_resource_profile(result, path).written
    saved = load_opcode_profile(path)
    assert (saved.resource, saved.own_object, saved.position) == (MOVED_RESOURCE, result.own_object, POSITION_FACING)


def test_unchanged_opcodes_are_confirmed(tmp_path):
    frames = [position(ME, source=MOVE_FACING, at=1.0 + n) for n in range(12)]
    frames += [position(ME, source=UPDATE, at=1.0 + n) for n in range(12)]
    result = relocate_position_replay(capture(tmp_path, frames), profile=profile(tmp_path))
    assert not result.changed


def test_too_little_movement_names_what_to_capture(tmp_path):
    with pytest.raises(LayoutRelocationError, match="move around for longer"):
        relocate_position_replay(capture(tmp_path, walking(5)), profile=profile(tmp_path))


def test_resource_naming_only_mounts_is_not_accepted(tmp_path):
    frames = [resource(MOUNT, current=32765 - n, change=-1, maximum=33637, layout=MOVED_RESOURCE, at=1.0 + n)
              for n in range(6)]
    with pytest.raises(LayoutRelocationError, match="use skills"):
        relocate_resource_replay(capture(tmp_path, frames), profile=profile(tmp_path))


def test_an_opcode_also_sent_at_another_length_is_no_candidate(tmp_path):
    frames = walking() + [frame(message(MOVED_RESOURCE.opcode, 50), at=30.0)]
    with pytest.raises(LayoutRelocationError, match="Resource"):
        relocate_resource_replay(capture(tmp_path, frames), profile=profile(tmp_path))


def test_layouts_relocation_cannot_check_are_refused(tmp_path):
    without_facing = PositionProfileLayout((MOVE,))
    with pytest.raises(LayoutRelocationError, match="no facing offset"):
        relocate_position_replay(capture(tmp_path, walking()), profile=profile(tmp_path, position=without_facing))
    other_source = OwnObjectProfileLayout((OwnObjectSource(0x1938, 51, 27),))
    with pytest.raises(LayoutRelocationError, match="not the resource message"):
        relocate_resource_replay(capture(tmp_path, walking()), profile=profile(tmp_path, own_object=other_source))


def test_profile_writes_are_guarded(tmp_path):
    result = relocate_resource_replay(capture(tmp_path, walking()), profile=profile(tmp_path))
    path = tmp_path / "profile.json"
    with pytest.raises(ProfileError, match="changed during"):
        update_resource_profile(result, path, expected_profile_sha256="0" * 64)
    data = json.loads(path.read_text())
    data["resource"]["opcode"] = "0x4444"
    path.write_text(json.dumps(data))
    with pytest.raises(ProfileError, match="not the one this calibration started from"):
        update_resource_profile(result, path)
