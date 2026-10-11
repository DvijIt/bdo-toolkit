"""Spawns, hits and kills: optional profile layout, record-list decoding, de-duplication, replay."""
from dataclasses import replace
import json

import pytest

import bdo_toolkit
from bdo_toolkit import load_opcode_profile
from bdo_toolkit.combat import CombatHit, CombatKill, CombatSpawn, CombatTrackingError, LiveCombatSession, replay_combat
from bdo_toolkit.combat._tracker import CombatTracker
from bdo_toolkit.profiles import ProfileError
from _support.combat import (BEE, BEETLE, COMBAT, FLOW, GRASS_BEETLE, HIT, KILL, ME, OTHER_PLAYER, PARASITIC_BEE,
                             POSITION, frame, hit_record, hits, kill_record, kills, message, profile_file,
                             spawn_record, spawns, write_capture)


def tracker():
    events = []
    return CombatTracker(COMBAT, events.append), events


def test_layout_roundtrip(tmp_path):
    profile = load_opcode_profile(profile_file(tmp_path))
    assert profile.combat == COMBAT
    assert profile.to_dict()["combat"]["hit"] == {
        "opcode": "0x1025", "header_length": 10, "count_offset": 8, "record_length": 109, "flag": 0,
        "target_id_offset": 30, "hit_id_offset": 34, "damage_offset": 42, "target_hp_offset": 54,
        "attacker_id_offset": 58, "position_offset": 62, "skill_offset": 95, "critical_offset": 103,
        "special_attack_offset": 102}
    assert "combat" not in replace(profile, combat=None).to_dict()


@pytest.mark.parametrize("section, change", [
    ("hit", {"skill_offset": 70}),            # overlaps the position
    ("kill", {"victim_id_offset": 186}),      # extends outside the record
    ("spawn", {"count_offset": 14}),          # outside the header
    ("spawn", {"opcode": "0x1025"}),          # same opcode as hits
    ("hit", {"flag": 1}),
    ("kill", {"extra": 1}),
    ("spawn", {"opcode": "nope"}),
    (None, {"encoding": "x"}),
])
def test_invalid_layout_rejected(tmp_path, section, change):
    path = profile_file(tmp_path)
    data = json.loads(path.read_text())
    (data["combat"][section] if section else data["combat"]).update(change)
    path.write_text(json.dumps(data))
    with pytest.raises(ProfileError):
        load_opcode_profile(path)


def test_spawn_names_the_hit_target_and_kill():
    t, events = tracker()
    t.observe(spawns(spawn_record(BEE, PARASITIC_BEE), spawn_record(BEETLE, GRASS_BEETLE)))
    t.observe(hits(hit_record(BEE, damage=6397.875, critical=1, special=0), at=2))
    t.observe(kills(kill_record(BEE), at=2))
    assert [type(e) for e in events] == [CombatSpawn, CombatSpawn, CombatHit, CombatKill]
    hit = events[2]
    assert (hit.target_id, hit.attacker_id, hit.skill_no, hit.damage, hit.target_hp) == (BEE, ME, 2621, 6397.875, 0)
    assert hit.is_critical and hit.is_back_attack and hit.is_lethal
    assert hit.target_character_key == PARASITIC_BEE and (hit.x, hit.y, hit.z) == POSITION
    kill = events[3]
    assert (kill.victim_id, kill.killer_id, kill.character_key) == (BEE, ME, PARASITIC_BEE)
    assert t.snapshot().status == "tracking" and t.snapshot().known_objects == 2


def test_plain_hit_on_a_target_seen_before_the_capture():
    t, events = tracker()
    t.observe(hits(hit_record(BEETLE, damage=1761.125, hp=8000.0), hit_record(BEE, attacker=OTHER_PLAYER)))
    plain, other = events
    assert not plain.is_critical and not plain.is_back_attack and not plain.is_lethal
    assert plain.target_character_key is None
    assert other.attacker_id == OTHER_PLAYER
    assert plain.to_dict()["target_id"] == "0x0D712C00" and plain.to_dict()["event_type"] == "combat_hit"


def test_air_attack():
    t, events = tracker()
    t.observe(hits(hit_record(BEE, damage=265.75, hp=99999616.0, skill=7185, special=4)))
    (hit,) = events
    assert hit.is_air_attack and not hit.is_back_attack and hit.to_dict()["is_air_attack"]


def test_repeats_and_records_without_damage_are_dropped():
    t, events = tracker()
    t.observe(spawns(spawn_record(BEE, PARASITIC_BEE)))
    t.observe(spawns(spawn_record(BEE, PARASITIC_BEE), spawn_record(BEETLE, GRASS_BEETLE)))
    t.observe(hits(hit_record(BEE, hit_id=0)))
    t.observe(kills(kill_record(BEE)))
    t.observe(kills(kill_record(BEE), kill_record(BEETLE, key=GRASS_BEETLE), at=40))
    assert [(type(e).__name__, getattr(e, "object_id", getattr(e, "victim_id", None))) for e in events] == [
        ("CombatSpawn", BEE), ("CombatSpawn", BEETLE), ("CombatKill", BEE), ("CombatKill", BEETLE)]


def test_a_new_connection_epoch_forgets_spawns_and_kills():
    t, events = tracker()
    t.observe(spawns(spawn_record(BEE, PARASITIC_BEE)))
    t.observe(kills(kill_record(BEE)))
    t.observe(hits(hit_record(BEE), epoch=2))
    t.observe(kills(kill_record(BEE), epoch=2))
    assert events[2].target_character_key is None and isinstance(events[3], CombatKill)
    t.close_flow(FLOW)
    assert t.snapshot().known_objects == 0


@pytest.mark.parametrize("raw", [
    message(HIT, [hit_record()], count=2),                    # count does not match the length
    message(HIT, [hit_record()])[:-1],                        # not a whole record
    message(HIT, [hit_record()], flag=1),
    message(HIT, [hit_record(damage=float("nan"))]),
    message(HIT, [hit_record(hp=-1.0)]),
    message(KILL, [[(KILL.position_offset, "3f", (float("inf"), 0.0, 0.0))]]),
])
def test_bad_shape_or_values_withdraw_authority(raw):
    t, _ = tracker()
    with pytest.raises(CombatTrackingError):
        t.observe(frame(raw))
    assert t.snapshot().status == "invalid"
    with pytest.raises(CombatTrackingError):
        t.observe(spawns(spawn_record(BEE, PARASITIC_BEE)))


def test_replay(tmp_path):
    frames = [spawns(spawn_record(BEE, PARASITIC_BEE)), hits(hit_record(BEE), at=2), kills(kill_record(BEE), at=2)]
    path = tmp_path / "capture.pcap"
    write_capture(path, frames)
    with replay_combat(path, profile=load_opcode_profile(profile_file(tmp_path))) as replay:
        events = list(replay)
        assert [type(e) for e in events] == [CombatSpawn, CombatHit, CombatKill]
        assert events[1].target_character_key == PARASITIC_BEE
        assert replay.status.status == "tracking" and replay.health.messages_observed == 3


def test_missing_layout_rejected(tmp_path):
    with pytest.raises(ProfileError, match="no combat layout"):
        LiveCombatSession(profile=load_opcode_profile(profile_file(tmp_path, combat=None)))


def test_public_export():
    assert bdo_toolkit.combat.replay_combat is replay_combat
    assert "combat" in bdo_toolkit.__all__
