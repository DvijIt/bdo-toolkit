"""Find a known combat layout again after a patch moved its opcodes, record shapes or field offsets.

Nothing of the previous layout is trusted except as a tie breaker. Record
list geometry comes from message lengths and the count byte (``_geometry``).
Each field is accepted only when its values pass checks that random data does
not (``_fields``): a target's HP falls by exactly the damage, a kill names
the target of a lethal hit at that moment, the killer is the lethal hit's
attacker, the kill happens where the hit landed, and a spawned object is hit
or killed later with the same character key. Fields with no such check (hit
ID, skill, critical, special attack) move with their nearest checked field
and must still read sensibly.
"""
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import NoReturn

from .._protocol import BDOFrame, FlowKey
from ..profiles import (CombatHitLayout, CombatKillLayout, CombatProfileLayout, CombatSpawnLayout,
                        ProfileError)
from ._fields import (MIN_SHARE, HitAmounts, Record, hit_amounts, id_offsets, kill_pairs, lethal_hits, near_share,
                      overlap, position_offsets, records, same_id_share, spawn_links)
from ._geometry import Geometry, fallback_geometries, solved_geometries
from .models import CombatTrackingError

# Bounds on what one calibration capture may hold (a few minutes of grinding).
MAX_CALIBRATION_MESSAGES = 100_000
MAX_CALIBRATION_BYTES = 128 * 1024 * 1024

MIN_KILLS = 3
MIN_KILL_AGREEMENT = 0.8      # of the distinct targets of lethal hits
MIN_KILLER_AGREEMENT = 0.6    # a summon's lethal hit is credited to its player (13 of 63 at the Orc Camp)
MIN_ATTACKER_AS_TARGET = 0.3  # of hits whose attacker is also hit in the capture
MIN_POSITION_AGREEMENT = 0.8
MIN_SPAWN_LINKS = 3
MIN_KEY_PAIRS = 3             # spawned objects killed later, for the character key
CRITICAL_VALUES = frozenset({0, 1})
SPECIAL_ATTACK_VALUES = frozenset({0, 2, 4, 6})   # back, down, air, plain


class CombatCalibrationError(CombatTrackingError):
    """The previous combat layout could not be found again; no profile was written."""


@dataclass(frozen=True)
class CombatRelocationEvidence:
    """Why each relocated message and field was accepted."""

    hit_pairs: int
    hit_agreement: float
    lethal_targets: int
    kills_matched: int
    spawn_links: int
    killer_agreement: float
    position_agreement: float

    def to_dict(self) -> dict[str, object]:
        return {"hit_pairs": self.hit_pairs, "hit_agreement": self.hit_agreement,
                "lethal_targets": self.lethal_targets, "kills_matched": self.kills_matched,
                "spawn_links": self.spawn_links, "killer_agreement": self.killer_agreement,
                "position_agreement": self.position_agreement}


@dataclass(frozen=True)
class CombatRelocationStatus:
    """``collecting`` while server messages are kept; ``invalid`` once a bound was hit."""

    status: str
    candidate_messages: int
    reason: str | None = None


class RelocationCollector:
    """Keeps every plain server message; which ones are record lists is found afterwards."""

    def __init__(self, previous: CombatProfileLayout) -> None:
        self.previous = previous
        self.frames: list[BDOFrame] = []
        self._bytes = 0
        self.reason: str | None = None

    def snapshot(self) -> CombatRelocationStatus:
        return CombatRelocationStatus("invalid" if self.reason else "collecting", len(self.frames), self.reason)

    def failed_snapshot(self, reason: str) -> CombatRelocationStatus:
        return CombatRelocationStatus("invalid", len(self.frames), reason)

    def invalidate(self, reason: str) -> NoReturn:
        self.reason = self.reason or reason
        raise CombatCalibrationError(self.reason)

    def observe(self, frame: BDOFrame) -> None:
        if self.reason:
            raise CombatCalibrationError(self.reason)
        if frame.flag != 0 or frame.length != len(frame.message):
            return
        self._bytes += len(frame.message)
        if len(self.frames) >= MAX_CALIBRATION_MESSAGES or self._bytes > MAX_CALIBRATION_BYTES:
            self.invalidate("Calibration capture holds too many messages; use a shorter capture")
        self.frames.append(frame)

    def close_flow(self, flow: FlowKey) -> None:
        pass  # Records are matched per flow epoch, so closed flows need no cleanup.


@dataclass(frozen=True)
class _Candidate:
    """One opcode read as a record list of one geometry."""

    opcode: int
    geometry: Geometry
    rows: list[Record]


@dataclass(frozen=True)
class _HitKill:
    hit: _Candidate
    amounts: HitAmounts
    kill: _Candidate
    victim: int
    pairs: list[tuple[Record, Record]]
    lethal: int


def _candidates(by_opcode: dict[int, list[BDOFrame]], solved: dict[int, tuple[Geometry, ...]],
                previous: Geometry, skip: Sequence[int] = ()) -> list[_Candidate]:
    found: list[_Candidate] = []
    for opcode, frames in by_opcode.items():
        if opcode in skip:
            continue
        geometries = solved[opcode] or fallback_geometries([f.message for f in frames], previous)
        found.extend(_Candidate(opcode, g, records(frames, g)) for g in geometries)
    return found


def _nearest[T](options: Sequence[T], distance: Callable[[T], int | tuple[int, ...]]) -> T:
    """The option closest to the previous layout; equally good options decode the same values."""
    return min(options, key=distance)


def _one_message[T](kind: str, options: Sequence[T], opcode: Callable[[T], str],
                    record_change: Callable[[T], int]) -> list[T]:
    """The options of the message whose record length changed least; a patch moves a few bytes, not a whole record.

    Another message that passes every check (0x1435 also names spawned
    objects with their character key) loses on length; two that tie are
    ambiguous.
    """
    least = min(map(record_change, options))
    closest = [option for option in options if record_change(option) == least]
    opcodes = sorted({opcode(option) for option in closest})
    if len(opcodes) > 1:
        raise CombatCalibrationError(f"Combat {kind}: several messages pass the checks ({', '.join(opcodes)}); "
                                     "capture more combat")
    return closest


def _hex(opcode: int) -> str:
    return f"0x{opcode:04X}"


def _find_hit_and_kill(by_opcode: dict[int, list[BDOFrame]], solved: dict[int, tuple[Geometry, ...]],
                       previous: CombatProfileLayout) -> _HitKill:
    hits = [(c, a) for c in _candidates(by_opcode, solved, Geometry.of(previous.hit)) for a in hit_amounts(c.rows)]
    if not hits:
        raise CombatCalibrationError("Combat hit: no message has a target whose HP falls by the damage; fight "
                                     "during the capture")
    kills = _candidates(by_opcode, solved, Geometry.of(previous.kill))
    options = []
    for hit, amounts in hits:
        lethal = lethal_hits(hit.rows, amounts)
        for kill in kills:
            if kill.opcode == hit.opcode or not lethal:
                continue
            for victim in id_offsets(kill.rows):
                pairs = kill_pairs(lethal, kill.rows, victim)
                if len(pairs) >= MIN_KILLS and len(pairs) >= MIN_KILL_AGREEMENT * len(lethal):
                    options.append(_HitKill(hit, amounts, kill, victim, pairs, len(lethal)))
    if not options:
        found = sorted({_hex(c.opcode) for c, _ in hits})
        raise CombatCalibrationError(f"Combat hit is one of {', '.join(found)}, but no kill message names the lethal "
                                     f"hits; kill at least {MIN_KILLS} monsters during the capture")
    h, k = previous.hit, previous.kill
    options = _one_message("hit and kill", options, lambda o: f"{_hex(o.hit.opcode)}/{_hex(o.kill.opcode)}",
                           lambda o: abs(o.hit.geometry.record_length - h.record_length)
                           + abs(o.kill.geometry.record_length - k.record_length))
    most = max(len(o.pairs) for o in options)
    # Windows a byte off on both sides match as well when the bytes around both IDs agree; the
    # target's own move breaks a tie in distance.
    return _nearest([o for o in options if len(o.pairs) == most], lambda o: (
        abs(o.amounts.target - h.target_id_offset) + abs(o.amounts.hp - h.target_hp_offset)
        + abs(o.amounts.damage - h.damage_offset) + abs(o.victim - k.victim_id_offset),
        abs(o.amounts.target - h.target_id_offset)))


def _best_pair(kind: str, options: list[tuple[int, int]], share: Callable[[int, int], float], minimum: float,
               previous: tuple[int, int]) -> tuple[tuple[int, int], float]:
    """The (hit offset, kill offset) with the highest share of agreeing pairs, nearest the previous on a tie."""
    scored = [(option, share(*option)) for option in options]
    passing = [(option, value) for option, value in scored if value >= minimum]
    if not passing:
        raise CombatCalibrationError(f"Combat {kind}: no field pair agrees on {minimum:.0%} of the kills; "
                                     "the record layout changed")
    best = max(value for _, value in passing)
    option = _nearest([o for o, value in passing if value == best],
                      lambda o: abs(o[0] - previous[0]) + abs(o[1] - previous[1]))
    return option, best


def _killer(match: _HitKill, previous: CombatProfileLayout) -> tuple[int, int, float]:
    """The hit's attacker and the kill's killer: the same object in most kills.

    A summon's lethal hit is credited to its player, so they agree on most
    kills, not all. Two junk fields that hold one constant would agree on
    every kill, so the attacker field must also name objects that are hit
    themselves (monsters hitting back, the player they hit): 68 to 78 % of
    hits at the Orc Camp, none for junk. Without that (a player who
    one-shots everything), only the previous offsets moved with their
    neighbours are accepted.
    """
    hit, kill = previous.hit, previous.kill

    def killer_share(attacker_offset: int, killer_offset: int) -> float:
        return same_id_share(match.pairs, attacker_offset, killer_offset)

    taken_hit, taken_kill = match.amounts.target, match.victim
    targets = {r.u32(taken_hit) for r in match.hit.rows}
    attackers = [a for a in id_offsets(match.hit.rows, varying=False) if not overlap(a, 4, taken_hit, 4)
                 and sum(r.u32(a) in targets for r in match.hit.rows) >= MIN_ATTACKER_AS_TARGET * len(match.hit.rows)]
    kill_offsets = [k for k in id_offsets(match.kill.rows, varying=False) if not overlap(k, 4, taken_kill, 4)]
    options = [(a, k) for a in attackers for k in kill_offsets]
    if any(killer_share(*option) >= MIN_KILLER_AGREEMENT for option in options):
        (attacker, killer), share = _best_pair("killer", options, killer_share, MIN_KILLER_AGREEMENT,
                                               (hit.attacker_id_offset, kill.killer_id_offset))
        return attacker, killer, share
    attacker = _moved(hit.attacker_id_offset, {hit.target_id_offset: taken_hit, hit.damage_offset: match.amounts.damage,
                                               hit.target_hp_offset: match.amounts.hp})
    killer = _moved(kill.killer_id_offset, {kill.victim_id_offset: taken_kill})
    in_range = attacker + 4 <= len(match.hit.rows[0].data) and killer + 4 <= len(match.kill.rows[0].data)
    if in_range and min(attacker, killer) >= 0 and killer_share(attacker, killer) >= MIN_KILLER_AGREEMENT:
        return attacker, killer, killer_share(attacker, killer)
    raise CombatCalibrationError("Combat killer: no attacker field names the killer of most kills; capture a fight "
                                 "where monsters hit back")


def _positions(match: _HitKill, previous: CombatProfileLayout) -> tuple[int, int, float]:
    options = [(p, q) for p in position_offsets(match.hit.rows) for q in position_offsets(match.kill.rows)]
    (hit_position, kill_position), share = _best_pair(
        "position", options, lambda p, q: near_share(match.pairs, p, q), MIN_POSITION_AGREEMENT,
        (previous.hit.position_offset, previous.kill.position_offset))
    return hit_position, kill_position, share


@dataclass(frozen=True)
class _Spawn:
    candidate: _Candidate
    object_offset: int
    links: int
    key_offset: int
    kill_key_offset: int


def _find_spawn(by_opcode: dict[int, list[BDOFrame]], solved: dict[int, tuple[Geometry, ...]],
                previous: CombatProfileLayout, match: _HitKill) -> _Spawn:
    target, victim = match.amounts.target, match.victim
    later = ([(r.connection, r.u32(target), r.at) for r in match.hit.rows]
             + [(r.connection, r.u32(victim), r.at) for r in match.kill.rows])
    seen = {(connection, object_id) for connection, object_id, _ in later}
    options: list[_Spawn] = []
    for candidate in _candidates(by_opcode, solved, Geometry.of(previous.spawn),
                                 skip=(match.hit.opcode, match.kill.opcode)):
        width = candidate.geometry.record_length
        for offset in range(width - 3):
            if sum((r.connection, r.u32(offset)) in seen for r in candidate.rows) < MIN_SPAWN_LINKS:
                continue  # Cheap filter before the time-ordered link check.
            linked = spawn_links(candidate.rows, offset, later)
            if len(linked) >= MIN_SPAWN_LINKS:
                keys = _key_offsets(linked, match)
                options.extend(_Spawn(candidate, offset, len(linked), s, k) for s, k in keys)
    if not options:
        raise CombatCalibrationError(f"Combat spawn: no message names at least {MIN_SPAWN_LINKS} monsters that were "
                                     f"hit later and {MIN_KEY_PAIRS} that were killed with the same character key; "
                                     "walk into view of monsters during the capture")
    s, k = previous.spawn, previous.kill
    options = _one_message("spawn", options, lambda o: _hex(o.candidate.opcode),
                           lambda o: abs(o.candidate.geometry.record_length - s.record_length))
    most = max(o.links for o in options)
    return _nearest([o for o in options if o.links == most], lambda o: (
        abs(o.object_offset - s.object_id_offset) + abs(o.key_offset - s.character_key_offset)
        + abs(o.kill_key_offset - k.character_key_offset)))


def _key_offsets(linked: dict[tuple[tuple[FlowKey, int], int], Record], match: _HitKill) -> list[tuple[int, int]]:
    """(spawn key offset, kill key offset) whose u16 agree for every spawned object killed later.

    A key that never changes (0, or one monster kind) proves nothing, so it
    has to take two values over the pairs.
    """
    killed = {(k.connection, k.u32(match.victim)): k for k in match.kill.rows}
    pairs = [(spawn, killed[key]) for key, spawn in linked.items() if key in killed]
    if len(pairs) < MIN_KEY_PAIRS:
        return []
    first_spawn, first_kill = pairs[0]
    found = []
    for kill_offset in range(len(first_kill.data) - 1):
        needle = first_kill.data[kill_offset:kill_offset + 2]
        spawn_offset = first_spawn.data.find(needle)
        while spawn_offset != -1:
            values = {spawn.u16(spawn_offset) for spawn, _ in pairs}
            if (len(values) > 1 and 0 not in values
                    and all(spawn.u16(spawn_offset) == kill.u16(kill_offset) for spawn, kill in pairs)):
                found.append((spawn_offset, kill_offset))
            spawn_offset = first_spawn.data.find(needle, spawn_offset + 1)
    return found


def _moved(previous_offset: int, anchors: dict[int, int]) -> int:
    """A field with no check of its own moves with the checked field that sat nearest to it."""
    nearest = min(anchors, key=lambda old: abs(old - previous_offset))
    return previous_offset + anchors[nearest] - nearest


def _unchecked_hit_fields(rows: Sequence[Record], previous: CombatHitLayout, anchors: dict[int, int],
                          damage: int, attacker: int) -> dict[str, int]:
    fields = {name: _moved(getattr(previous, name), anchors)
              for name in ("hit_id_offset", "skill_offset", "critical_offset", "special_attack_offset")}
    width = len(rows[0].data)
    for name, value in fields.items():
        if not 0 <= value <= width - dict(CombatHitLayout.FIELDS)[name]:
            raise CombatCalibrationError(f"Combat hit {name.removesuffix('_offset')}: moved outside the record")
    damaging = [r for r in rows if r.f32(damage) >= 1]
    player = Counter(r.u32(attacker) for r in damaging).most_common(1)[0][0]
    own = [r for r in damaging if r.u32(attacker) == player]
    hit_id, skill = fields["hit_id_offset"], fields["skill_offset"]
    by_hit_id: dict[int, Counter[int]] = defaultdict(Counter)
    for row in damaging:
        by_hit_id[row.u32(hit_id)][row.u16(skill)] += 1
    checks = {
        "hit_id": sum(r.u32(hit_id) != 0 for r in damaging) / len(damaging),
        "skill": sum(c.most_common(1)[0][1] for c in by_hit_id.values()) / len(damaging),
        "critical": sum(r.data[fields["critical_offset"]] in CRITICAL_VALUES for r in own) / len(own),
        "special_attack": sum(r.data[fields["special_attack_offset"]] in SPECIAL_ATTACK_VALUES for r in own) / len(own),
    }
    for name, share in checks.items():
        if share < MIN_SHARE:
            raise CombatCalibrationError(f"Combat hit {name}: the field moved with its neighbours reads sensibly in "
                                         f"only {share:.0%} of hits; the record layout changed around it")
    return fields


def relocate(collector: RelocationCollector) -> tuple[CombatProfileLayout, CombatRelocationEvidence]:
    """The combat layout found again in the collected messages, and the evidence for it."""
    previous = collector.previous
    by_opcode: dict[int, list[BDOFrame]] = defaultdict(list)
    for frame in collector.frames:
        by_opcode[frame.opcode].append(frame)
    solved = {opcode: solved_geometries([f.message for f in frames]) for opcode, frames in by_opcode.items()}

    match = _find_hit_and_kill(by_opcode, solved, previous)
    attacker, killer, killer_share = _killer(match, previous)
    hit_position, kill_position, position_share = _positions(match, previous)
    spawn = _find_spawn(by_opcode, solved, previous, match)

    old_hit = previous.hit
    anchors = {old_hit.target_id_offset: match.amounts.target, old_hit.damage_offset: match.amounts.damage,
               old_hit.target_hp_offset: match.amounts.hp, old_hit.attacker_id_offset: attacker,
               old_hit.position_offset: hit_position}
    unchecked = _unchecked_hit_fields(match.hit.rows, old_hit, anchors, match.amounts.damage, attacker)
    try:
        layout = CombatProfileLayout(
            spawn=CombatSpawnLayout(spawn.candidate.opcode, *_geometry_fields(spawn.candidate.geometry),
                                    character_key_offset=spawn.key_offset, object_id_offset=spawn.object_offset),
            hit=CombatHitLayout(match.hit.opcode, *_geometry_fields(match.hit.geometry),
                                target_id_offset=match.amounts.target, damage_offset=match.amounts.damage,
                                target_hp_offset=match.amounts.hp, attacker_id_offset=attacker,
                                position_offset=hit_position, **unchecked),
            kill=CombatKillLayout(match.kill.opcode, *_geometry_fields(match.kill.geometry),
                                  position_offset=kill_position, character_key_offset=spawn.kill_key_offset,
                                  killer_id_offset=killer, victim_id_offset=match.victim))
    except ProfileError as exc:
        raise CombatCalibrationError(f"The combat layout found does not validate: {exc}") from exc
    amounts = match.amounts
    evidence = CombatRelocationEvidence(amounts.agree, round(amounts.agreement, 4), match.lethal, len(match.pairs),
                                        spawn.links, round(killer_share, 4), round(position_share, 4))
    return layout, evidence


def _geometry_fields(geometry: Geometry) -> tuple[int, int, int]:
    return geometry.header_length, geometry.count_offset, geometry.record_length
