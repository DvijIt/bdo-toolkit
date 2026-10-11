"""Re-find a known combat layout after a patch moved its opcodes.

The record shapes and field offsets of the previous layout are kept; only
the opcodes are searched. A candidate message is accepted only when its
fields pass checks that random data does not: a target's HP falls by
exactly the damage, a kill names the target of a lethal hit at that moment,
and a spawned object is hit or killed later with the same character key.
"""
import math
import struct
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import NoReturn

from .._protocol import BDOFrame, FlowKey
from ..profiles import CombatHitLayout, CombatKillLayout, CombatProfileLayout, CombatSpawnLayout
from .models import CombatTrackingError

# Bounds on what one calibration capture may hold (a few minutes of grinding).
MAX_CALIBRATION_MESSAGES = 50_000
MAX_CALIBRATION_BYTES = 128 * 1024 * 1024

MIN_HIT_PAIRS = 5          # consecutive hits on one target whose HP drop matched the damage
MIN_HIT_AGREEMENT = 0.9    # other players' hits can repeat a stale HP; a wrong field never agrees
MIN_KILLS = 3
MIN_KILL_AGREEMENT = 0.8   # of the distinct targets of lethal hits
MIN_SPAWN_LINKS = 3
KILL_WINDOW_SECONDS = 0.5

_U16 = struct.Struct("<H")
_U32 = struct.Struct("<I")
_F32 = struct.Struct("<f")
_XYZ = struct.Struct("<3f")

type RecordLayout = CombatSpawnLayout | CombatHitLayout | CombatKillLayout


class CombatCalibrationError(CombatTrackingError):
    """The previous combat layout could not be re-found; no profile was written."""


@dataclass(frozen=True)
class CombatRelocationEvidence:
    """Why each relocated message was accepted."""

    hit_pairs: int
    hit_agreement: float
    lethal_targets: int
    kills_matched: int
    spawn_links: int

    def to_dict(self) -> dict[str, object]:
        return {"hit_pairs": self.hit_pairs, "hit_agreement": self.hit_agreement,
                "lethal_targets": self.lethal_targets, "kills_matched": self.kills_matched,
                "spawn_links": self.spawn_links}


@dataclass(frozen=True)
class CombatRelocationStatus:
    """``collecting`` while candidate messages are kept; ``invalid`` once a bound was hit."""

    status: str
    candidate_messages: int
    reason: str | None = None


def fits(layout: RecordLayout, frame: BDOFrame) -> bool:
    """The message has the layout's record list shape, whatever its opcode."""
    message = frame.message
    body = len(message) - layout.header_length
    return (frame.flag == layout.flag and frame.length == len(message) and body > 0
            and body % layout.record_length == 0 and message[layout.count_offset] == body // layout.record_length)


class RelocationCollector:
    """Keeps the server messages that fit one of the previous layout's record shapes.

    An opcode with even one message that does not fit a shape cannot be that
    message, so it is struck off for that kind.
    """

    def __init__(self, previous: CombatProfileLayout) -> None:
        self.previous = previous
        self.frames: list[BDOFrame] = []
        self.misfits: dict[str, set[int]] = {"spawn": set(), "hit": set(), "kill": set()}
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
        if frame.flag != 0:
            return
        fitting = False
        for kind, layout in self._layouts():
            if fits(layout, frame):
                fitting = True
            else:
                self.misfits[kind].add(frame.opcode)
        if fitting:
            self._bytes += len(frame.message)
            if len(self.frames) >= MAX_CALIBRATION_MESSAGES or self._bytes > MAX_CALIBRATION_BYTES:
                self.invalidate("Calibration capture holds too many candidate messages; use a shorter capture")
            self.frames.append(frame)

    def close_flow(self, flow: FlowKey) -> None:
        pass  # Records are matched per flow epoch, so closed flows need no cleanup.

    def _layouts(self) -> tuple[tuple[str, RecordLayout], ...]:
        p = self.previous
        return (("spawn", p.spawn), ("hit", p.hit), ("kill", p.kill))

    def candidates(self, kind: str, layout: RecordLayout) -> dict[int, list[BDOFrame]]:
        found: dict[int, list[BDOFrame]] = defaultdict(list)
        for frame in self.frames:
            if frame.opcode not in self.misfits[kind] and fits(layout, frame):
                found[frame.opcode].append(frame)
        return found


@dataclass(frozen=True)
class _Hit:
    at: float
    connection: tuple[FlowKey, int]
    target: int
    attacker: int
    damage: float
    hp: float


@dataclass(frozen=True)
class _Kill:
    at: float
    connection: tuple[FlowKey, int]
    victim: int
    killer: int
    key: int


def _records(frames: Iterable[BDOFrame], layout: RecordLayout) -> Iterable[tuple[BDOFrame, int]]:
    for frame in frames:
        for start in range(layout.header_length, len(frame.message), layout.record_length):
            yield frame, start


def _connection(frame: BDOFrame) -> tuple[FlowKey, int]:
    return frame.context.flow, frame.context.flow_generation


def _decode_hits(frames: list[BDOFrame], layout: CombatHitLayout) -> list[_Hit] | None:
    """Hits as the decoder would read them; None when a value would make it fail."""
    hits = []
    for frame, start in _records(frames, layout):
        message = frame.message
        if _U32.unpack_from(message, start + layout.hit_id_offset)[0] == 0:
            continue
        (damage,) = _F32.unpack_from(message, start + layout.damage_offset)
        (hp,) = _F32.unpack_from(message, start + layout.target_hp_offset)
        position = _XYZ.unpack_from(message, start + layout.position_offset)
        if not all(math.isfinite(v) for v in (damage, hp, *position)) or damage < 0 or hp < 0:
            return None
        hits.append(_Hit(frame.context.timestamp, _connection(frame),
                         _U32.unpack_from(message, start + layout.target_id_offset)[0],
                         _U32.unpack_from(message, start + layout.attacker_id_offset)[0], damage, hp))
    return hits


def _hp_agreement(hits: list[_Hit]) -> tuple[int, int]:
    """Consecutive hits on one target whose HP drop matches (and does not match) the damage."""
    last: dict[tuple[tuple[FlowKey, int], int], float] = {}
    agree = disagree = 0
    for hit in hits:
        key = (hit.connection, hit.target)
        before = last.get(key)
        last[key] = hit.hp
        if before is None or hit.hp < 1 or hit.damage < 1 or before - hit.hp < 1:
            continue  # Kills, misses and repeated (stale) HP say nothing.
        if abs(before - hit.hp - hit.damage) <= 1 + 0.002 * hit.damage:
            agree += 1
        else:
            disagree += 1
    return agree, disagree


def _decode_kills(frames: list[BDOFrame], layout: CombatKillLayout) -> list[_Kill] | None:
    kills = []
    for frame, start in _records(frames, layout):
        message = frame.message
        if not all(math.isfinite(v) for v in _XYZ.unpack_from(message, start + layout.position_offset)):
            return None
        kills.append(_Kill(frame.context.timestamp, _connection(frame),
                           _U32.unpack_from(message, start + layout.victim_id_offset)[0],
                           _U32.unpack_from(message, start + layout.killer_id_offset)[0],
                           _U16.unpack_from(message, start + layout.character_key_offset)[0]))
    return kills


def _kill_matches(kills: list[_Kill], hits: list[_Hit]) -> tuple[int, int]:
    """Distinct lethal-hit targets, and how many of them a kill names at the same moment.

    The killer is not compared. A helper object's lethal hit is credited to
    the player: 13 of 63 Wizard kills at the Orc Camp came from objects the
    spawns name Keeper Marg, Keeper Arne and Flame's Calling, with their own
    skill numbers.
    """
    lethal = {(h.connection, h.target): h for h in hits if h.hp <= 0 and h.damage >= 1}
    matched = sum(any(k.connection == c and k.victim == target and abs(k.at - hit.at) <= KILL_WINDOW_SECONDS
                      for k in kills)
                  for (c, target), hit in lethal.items())
    return len(lethal), matched


def _spawn_links(frames: list[BDOFrame], layout: CombatSpawnLayout, hits: list[_Hit],
                 kills: list[_Kill]) -> int | None:
    """Spawned objects later hit or killed; None when a kill names another character key."""
    spawned: dict[tuple[tuple[FlowKey, int], int], tuple[float, int]] = {}
    for frame, start in _records(frames, layout):
        object_id = _U32.unpack_from(frame.message, start + layout.object_id_offset)[0]
        key = _U16.unpack_from(frame.message, start + layout.character_key_offset)[0]
        spawned.setdefault((_connection(frame), object_id), (frame.context.timestamp, key))
    linked = set()
    for hit in hits:
        entry = spawned.get((hit.connection, hit.target))
        if entry is not None and entry[0] <= hit.at:
            linked.add((hit.connection, hit.target))
    for kill in kills:
        entry = spawned.get((kill.connection, kill.victim))
        if entry is not None and entry[0] <= kill.at:
            if entry[1] != kill.key:
                return None
            linked.add((kill.connection, kill.victim))
    return len(linked)


def _single(kind: str, passing: list[int], tried: int) -> int:
    if len(passing) == 1:
        return passing[0]
    if passing:
        raise CombatCalibrationError(f"Combat {kind}: {len(passing)} messages pass the checks "
                                     f"({', '.join(f'0x{o:04X}' for o in passing)}); capture more combat")
    raise CombatCalibrationError(f"Combat {kind}: none of {tried} candidate messages pass the checks; "
                                 "the record layout may have changed")


def relocate(collector: RelocationCollector) -> tuple[CombatProfileLayout, CombatRelocationEvidence]:
    """The previous layout with each opcode re-found, and the evidence for it."""
    previous = collector.previous

    hit_candidates: dict[int, list[_Hit]] = {}
    for opcode, frames in collector.candidates("hit", previous.hit).items():
        decoded = _decode_hits(frames, replace(previous.hit, opcode=opcode))
        if decoded is not None:
            hit_candidates[opcode] = decoded
    hit_agreement = {opcode: _hp_agreement(decoded) for opcode, decoded in hit_candidates.items()}
    hp_passing = [opcode for opcode, (agree, disagree) in hit_agreement.items()
                  if agree >= MIN_HIT_PAIRS and agree >= MIN_HIT_AGREEMENT * (agree + disagree)]
    if not hit_candidates:
        raise CombatCalibrationError("Combat hit: no message has the previous hit record shape; fight during "
                                     "the capture, or the record layout changed")

    kill_candidates: dict[int, list[_Kill]] = {}
    for opcode, frames in collector.candidates("kill", previous.kill).items():
        decoded_kills = _decode_kills(frames, replace(previous.kill, opcode=opcode))
        if decoded_kills is not None:
            kill_candidates[opcode] = decoded_kills
    # Hits and kills confirm each other: a lethal hit's target is a kill's victim at the same moment.
    pairs: dict[tuple[int, int], tuple[int, int]] = {}
    for hit_op, decoded in hit_candidates.items():
        agree, disagree = hit_agreement[hit_op]
        if agree + disagree >= MIN_HIT_PAIRS and agree < MIN_HIT_AGREEMENT * (agree + disagree):
            continue  # Enough repeated hits to tell, and the HP does not fall by the damage.
        for kill_op, decoded_kills in kill_candidates.items():
            if kill_op == hit_op:
                continue
            lethal, matched = _kill_matches(decoded_kills, decoded)
            if matched >= MIN_KILLS and matched >= MIN_KILL_AGREEMENT * lethal:
                pairs[(hit_op, kill_op)] = (lethal, matched)
    if not pairs:
        found = f"Combat hit is 0x{hp_passing[0]:04X}, but no" if len(hp_passing) == 1 else "No"
        raise CombatCalibrationError(f"{found} kill message names the lethal hits; kill at least {MIN_KILLS} "
                                     "monsters during the capture, or the record layout changed")
    if len(pairs) > 1:
        raise CombatCalibrationError("Combat hit and kill: several message pairs pass the checks ("
                                     + ", ".join(f"0x{h:04X}/0x{k:04X}" for h, k in pairs) + "); capture more combat")
    ((hit_opcode, kill_opcode),) = pairs
    lethal, matched = pairs[(hit_opcode, kill_opcode)]
    hits, kills = hit_candidates[hit_opcode], kill_candidates[kill_opcode]
    agree, disagree = hit_agreement[hit_opcode]

    spawn_results: dict[int, int] = {}
    spawn_candidates = collector.candidates("spawn", previous.spawn)
    for opcode, frames in spawn_candidates.items():
        if opcode in (hit_opcode, kill_opcode):
            continue
        links = _spawn_links(frames, replace(previous.spawn, opcode=opcode), hits, kills)
        if links is not None and links >= MIN_SPAWN_LINKS:
            spawn_results[opcode] = links
    if not spawn_results:
        raise CombatCalibrationError(f"Combat spawn: no message names at least {MIN_SPAWN_LINKS} monsters that "
                                     "were hit or killed later; walk into view of monsters during the capture, "
                                     "or the record layout changed")
    spawn_opcode = _single("spawn", list(spawn_results), len(spawn_candidates))

    layout = replace(previous, spawn=replace(previous.spawn, opcode=spawn_opcode),
                     hit=replace(previous.hit, opcode=hit_opcode), kill=replace(previous.kill, opcode=kill_opcode))
    agreement = round(agree / (agree + disagree), 4) if agree + disagree else 0.0
    evidence = CombatRelocationEvidence(agree, agreement, lethal, matched, spawn_results[spawn_opcode])
    return layout, evidence
