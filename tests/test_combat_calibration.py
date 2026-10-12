"""Re-finding moved combat opcodes from a capture and saving them to a profile."""
from dataclasses import replace
import hashlib
import json

import pytest

from bdo_toolkit import load_opcode_profile
from bdo_toolkit._tracked_capture import CaptureHealth
from bdo_toolkit.combat import CombatCalibrationError, relocate_combat_replay, update_combat_profile
from bdo_toolkit.combat.calibration import CombatCalibrationResult
from bdo_toolkit.profiles import CombatProfileLayout, ProfileError
from _support.combat import (COMBAT, HIT, KILL, ME, SPAWN, frame, hit_record, kill_record, message, profile_file,
                             spawn_record, write_capture)

MOVED = replace(COMBAT, spawn=replace(SPAWN, opcode=0x2001), hit=replace(HIT, opcode=0x2002),
                kill=replace(KILL, opcode=0x2003))
MOBS = [0x0D834400 + 0x100 * n for n in range(5)]
KEYS = dict(zip(MOBS, (20001, 20002, 20001, 20003, 20002)))  # a fixed key proves nothing, so mob kinds vary


def fight(layout=MOVED, *, spawn=True, kill=True, extra=()):
    """Five mobs walk into view, take three hits each (HP 4000 to 0) and die."""
    frames = []
    at = 1.0
    if spawn:
        frames.append(frame(message(layout.spawn, [spawn_record(mob, KEYS[mob]) for mob in MOBS]), at=at))
    for mob in MOBS:
        for hp in (3000.0, 2000.0, 0.0):
            at += 0.5
            frames.append(frame(message(layout.hit, [hit_record(mob, damage=1000.0, hp=hp)]), at=at))
        if kill:
            frames.append(frame(message(layout.kill, [kill_record(mob, key=KEYS[mob])]), at=at))
    frames.extend(extra)
    return frames


def relocate(tmp_path, frames, combat=COMBAT):
    capture = tmp_path / "fight.pcap"
    write_capture(capture, frames)
    return relocate_combat_replay(capture, profile=load_opcode_profile(profile_file(tmp_path, combat=combat)))


def test_moved_opcodes_are_found_and_saved(tmp_path):
    decoys = [frame(message(replace(HIT, opcode=0x3001), [[]]), at=30.0),       # hit shape, 0xED filler
              frame(message(replace(HIT, opcode=0x3000), [[(HIT.hit_id_offset, "I", 0)]]), at=30.0)]
    result = relocate(tmp_path, fight(extra=decoys))
    assert result.layout == MOVED and result.changed and result.previous == COMBAT
    assert result.evidence.to_dict() == {"hit_pairs": 5, "hit_agreement": 1.0, "lethal_targets": 5,
                                         "kills_matched": 5, "spawn_links": 5, "killer_agreement": 1.0,
                                         "position_agreement": 1.0}
    path = tmp_path / "profile.json"
    update = update_combat_profile(result, path, expected_profile_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    assert update.written and update.backup_path is not None and update.backup_path.exists()
    assert load_opcode_profile(path).combat == MOVED
    assert not update_combat_profile(result, path).written


def test_unchanged_opcodes_are_confirmed(tmp_path):
    result = relocate(tmp_path, fight(COMBAT))
    assert result.layout == COMBAT and not result.changed


@pytest.mark.parametrize("kwargs, reason", [(dict(kill=False), "kill at least 3"),
                                            (dict(spawn=False), "walk into view")])
def test_missing_evidence_names_what_to_capture(tmp_path, kwargs, reason):
    with pytest.raises(CombatCalibrationError, match=reason):
        relocate(tmp_path, fight(**kwargs))


def test_two_kill_messages_that_both_fit_are_ambiguous(tmp_path):
    twin = replace(KILL, opcode=0x2004)
    copies = [frame(message(twin, [kill_record(mob, key=KEYS[mob])]), at=2.5 + 1.5 * n) for n, mob in enumerate(MOBS)]
    with pytest.raises(CombatCalibrationError, match="several"):
        relocate(tmp_path, fight(extra=copies))


def test_a_kill_long_after_the_lethal_hit_does_not_match(tmp_path):
    frames = fight(kill=False)
    frames += [frame(message(MOVED.kill, [kill_record(mob, key=KEYS[mob])]), at=40.0) for mob in MOBS]
    with pytest.raises(CombatCalibrationError, match="kill at least 3"):
        relocate(tmp_path, frames)


def test_a_kill_credited_to_the_player_matches_a_summons_lethal_hit(tmp_path):
    summon = 0x17A49CC4
    frames = []
    at = 1.0
    frames.append(frame(message(MOVED.spawn, [spawn_record(mob, KEYS[mob]) for mob in MOBS]), at=at))
    for mob in MOBS:
        finisher = summon if mob == MOBS[0] else ME  # 13 of 63 lethal hits at the Orc Camp were a summon's
        for hp, attacker in ((3000.0, ME), (2000.0, ME), (0.0, finisher)):
            at += 0.5
            frames.append(frame(message(MOVED.hit, [hit_record(mob, attacker=attacker, damage=1000.0, hp=hp)]), at=at))
        frames.append(frame(message(MOVED.kill, [kill_record(mob, killer=ME, key=KEYS[mob])]), at=at))
    assert relocate(tmp_path, frames).layout == MOVED


# A patch that inserted bytes: 3 into the spawn header, 4 into each hit record after the damage (HP and
# everything behind it move), 2 into each kill record before the killer (killer and victim move).
PATCHED = CombatProfileLayout(
    replace(SPAWN, opcode=0x2101, header_length=17),
    replace(HIT, opcode=0x2102, record_length=113, target_hp_offset=58, attacker_id_offset=62, position_offset=66,
            skill_offset=99, critical_offset=107, special_attack_offset=106),
    replace(KILL, opcode=0x2103, record_length=191, killer_id_offset=147, victim_id_offset=183))


def patched_fight(layout=PATCHED):
    """``fight`` on another layout, with a spawn of one and a hit on two, so both show two record counts."""
    spawn, hit, kill = layout.spawn, layout.hit, layout.kill
    frames = [frame(message(spawn, [spawn_record(mob, KEYS[mob], layout=spawn) for mob in MOBS[:4]]), at=1.0),
              frame(message(spawn, [spawn_record(MOBS[4], KEYS[MOBS[4]], layout=spawn)]), at=1.2),
              frame(message(hit, [hit_record(mob, damage=1000.0, hp=3000.0, layout=hit) for mob in MOBS[:2]]), at=1.5)]
    at = 1.5
    for mob in MOBS:
        for hp in (2000.0, 0.0) if mob in MOBS[:2] else (3000.0, 2000.0, 0.0):
            at += 0.5
            frames.append(frame(message(hit, [hit_record(mob, damage=1000.0, hp=hp, layout=hit)]), at=at))
        frames.append(frame(message(kill, [kill_record(mob, key=KEYS[mob], layout=kill)]), at=at))
    return frames


def test_moved_record_shapes_and_offsets_are_found(tmp_path):
    result = relocate(tmp_path, patched_fight())
    assert result.layout == PATCHED and result.previous == COMBAT
    assert result.evidence.hit_pairs == 5 and result.evidence.kills_matched == 5


def test_an_unchecked_field_that_did_not_move_with_its_neighbours_fails(tmp_path):
    stayed = replace(PATCHED, hit=replace(PATCHED.hit, critical_offset=HIT.critical_offset))
    with pytest.raises(CombatCalibrationError, match="critical"):
        relocate(tmp_path, patched_fight(stayed))


def test_profile_writes_are_guarded(tmp_path):
    result = relocate(tmp_path, fight())
    path = tmp_path / "profile.json"
    with pytest.raises(ProfileError, match="changed during"):
        update_combat_profile(result, path, expected_profile_sha256="0" * 64)
    data = json.loads(path.read_text())
    data["combat"]["hit"]["opcode"] = "0x4444"
    path.write_text(json.dumps(data))
    with pytest.raises(ProfileError, match="not the one this calibration started from"):
        update_combat_profile(result, path)


def test_result_requires_a_clean_capture(tmp_path):
    result = relocate(tmp_path, fight())
    with pytest.raises(CombatCalibrationError, match="clean"):
        replace(result, health=CaptureHealth(tcp_gap_resets=1))
    assert isinstance(result, CombatCalibrationResult)
