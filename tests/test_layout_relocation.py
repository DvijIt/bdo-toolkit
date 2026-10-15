"""Finding moved, resized and reordered position, own-object and resource layouts and saving them to a profile."""
from dataclasses import replace
import hashlib
import json

import pytest

from bdo_toolkit import load_opcode_profile
from bdo_toolkit.position import LayoutRelocationError, relocate_position_replay, update_position_profile
from bdo_toolkit.profiles import (OwnObjectProfileLayout, OwnObjectSource, PositionProfileLayout, PositionSource,
                                  ProfileError, ResourceProfileLayout)
from bdo_toolkit.resource import relocate_resource_replay, update_resource_profile
from _support.tracked import (ME, MOUNT, MOVE, MOVE_FACING, OTHER_PLAYER, OWN_OBJECT, POSITION_FACING, RESOURCE, UPDATE,
                              frame, message, position, profile_file, resource, write_capture)

# The first patch after client 3464: new opcodes, lengths and field order.
PATCHED_MOVE = PositionSource(0x2001, 41, 20, 32, facing_offset=8)
PATCHED_UPDATE = PositionSource(0x2002, 70, 5, 29, facing_offset=45)
PATCHED_RESOURCE = ResourceProfileLayout(0x2003, 49, 45, 25, 37, 41)
# The action message also holds positions and facing for every mover, at a length far from either source.
ACTION = PositionSource(0x2004, 166, 9, 136, facing_offset=116)
MAXIMUM = 1840
COST = 90


def walking(count: int = 12, *, sources=(PATCHED_MOVE, PATCHED_UPDATE, ACTION), layout=PATCHED_RESOURCE):
    """Two players walking in every source while the player spends resource and it refills."""
    frames = []
    for n in range(count):
        at = 1.0 + n
        for source in sources:
            frames.append(position(ME, 40429.0 + 50 * n, source=source, facing=(0.6, 0.0, 0.8), at=at))
            frames.append(position(OTHER_PLAYER, 41000.0 - 30 * n, source=source, facing=(-1.0, 0.0, 0.0), at=at))
        spent = n % 2 == 0
        frames.append(resource(ME, current=MAXIMUM - COST if spent else MAXIMUM, change=-COST if spent else COST,
                               maximum=MAXIMUM, layout=layout, at=at))
    return frames


def without_resource(frames):
    opcode = PATCHED_RESOURCE.opcode.to_bytes(2, "little")
    return [f for f in frames if f.message[3:5] != opcode]


def capture(tmp_path, frames, name="capture.pcap"):
    path = tmp_path / name
    write_capture(path, frames)
    return path


def profile(tmp_path, **layouts):
    defaults = dict(position=POSITION_FACING, own_object=OWN_OBJECT, resource=RESOURCE)
    return load_opcode_profile(profile_file(tmp_path, **{**defaults, **layouts}))


def test_position_sources_are_found_and_saved(tmp_path):
    result = relocate_position_replay(capture(tmp_path, walking()), profile=profile(tmp_path))
    assert result.layout == PositionProfileLayout((PATCHED_MOVE, PATCHED_UPDATE)) and result.changed
    assert result.readings == (24, 24)
    path = tmp_path / "profile.json"
    update = update_position_profile(result, path,
                                     expected_profile_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    assert update.written and update.backup_path.exists()
    saved = load_opcode_profile(path)
    assert saved.position == result.layout and saved.resource == RESOURCE
    assert not update_position_profile(result, path).written


def test_resource_and_its_own_object_source_move_together(tmp_path):
    result = relocate_resource_replay(capture(tmp_path, walking()), profile=profile(tmp_path))
    assert result.layout == PATCHED_RESOURCE and result.readings == 12
    assert result.own_object == OwnObjectProfileLayout((OwnObjectSource(0x2003, 49, 37),))
    path = tmp_path / "profile.json"
    assert update_resource_profile(result, path).written
    saved = load_opcode_profile(path)
    assert (saved.resource, saved.own_object, saved.position) == (PATCHED_RESOURCE, result.own_object, POSITION_FACING)


def test_unchanged_layouts_are_confirmed(tmp_path):
    path = capture(tmp_path, walking(sources=(MOVE_FACING, UPDATE), layout=RESOURCE))
    assert not relocate_position_replay(path, profile=profile(tmp_path)).changed
    assert not relocate_resource_replay(path, profile=profile(tmp_path)).changed


def test_a_source_without_facing_gains_one(tmp_path):
    result = relocate_position_replay(capture(tmp_path, walking()),
                                      profile=profile(tmp_path, position=PositionProfileLayout((MOVE,))))
    assert result.layout == PositionProfileLayout((PATCHED_MOVE,))


def test_too_little_movement_names_what_to_capture(tmp_path):
    with pytest.raises(LayoutRelocationError, match="move around"):
        relocate_position_replay(capture(tmp_path, walking(5)), profile=profile(tmp_path))


def test_resource_that_never_refills_is_not_accepted(tmp_path):
    frames = without_resource(walking())
    frames += [resource(ME, current=1754 - n, change=-1, layout=PATCHED_RESOURCE, at=1.0 + n) for n in range(12)]
    with pytest.raises(LayoutRelocationError, match="let it refill"):
        relocate_resource_replay(capture(tmp_path, frames), profile=profile(tmp_path))


def test_resource_naming_only_mounts_is_not_accepted(tmp_path):
    frames = without_resource(walking())
    frames += [resource(MOUNT, current=33637 - 10 * (n % 2 == 0), change=-10 if n % 2 == 0 else 10, maximum=33637,
                        layout=PATCHED_RESOURCE, at=1.0 + n) for n in range(12)]
    with pytest.raises(LayoutRelocationError, match="use skills"):
        relocate_resource_replay(capture(tmp_path, frames), profile=profile(tmp_path))


def test_an_opcode_also_sent_at_another_length_is_no_candidate(tmp_path):
    frames = walking() + [frame(message(PATCHED_RESOURCE.opcode, 50), at=30.0)]
    with pytest.raises(LayoutRelocationError, match="Resource"):
        relocate_resource_replay(capture(tmp_path, frames), profile=profile(tmp_path))


def test_sources_that_cannot_be_told_apart_are_refused(tmp_path):
    both_short = PositionProfileLayout((MOVE_FACING, replace(UPDATE, opcode=0x1A22, message_length=47)))
    with pytest.raises(LayoutRelocationError, match="nearest the same message"):
        relocate_position_replay(capture(tmp_path, walking()), profile=profile(tmp_path, position=both_short))


def test_own_object_sources_relocation_cannot_check_are_refused(tmp_path):
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
