"""Field offsets inside combat records, found by checks that random bytes fail.

Every search scans each offset of a record and keeps the ones whose values
pass a cross check between messages: a target's HP falls by the damage, a
kill names a lethal hit's target at that moment, a spawned object is hit or
killed later, and so on. Callers break ties by the previous offsets.
"""
import math
import struct
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from .._protocol import BDOFrame, FlowKey
from ._geometry import Geometry

MIN_HIT_PAIRS = 5          # consecutive hits on one target whose HP drop matched the damage
MIN_HIT_AGREEMENT = 0.9    # other players' hits can repeat a stale HP; a wrong field never agrees
KILL_WINDOW_SECONDS = 0.5
MIN_SHARE = 0.95           # of records a field must read sensibly in (a few records carry no damage)
MAX_KILL_DISTANCE_CM = 500.0
MIN_COORDINATE_CM = 100.0          # world x and z: game maps are hundreds of km wide, never this near the origin
MAX_COORDINATE_CM = 100_000_000.0
SAMPLE_ROWS = 400          # rows read to pick candidate offsets
SCREEN_ROWS = 300          # first records a (target, HP, damage) must agree on before all are read

_U16 = struct.Struct("<H")
_U32 = struct.Struct("<I")
_F32 = struct.Struct("<f")

type Connection = tuple[FlowKey, int]


@dataclass(frozen=True)
class Record:
    at: float
    connection: Connection
    data: bytes

    def u16(self, offset: int) -> int:
        return _U16.unpack_from(self.data, offset)[0]

    def u32(self, offset: int) -> int:
        return _U32.unpack_from(self.data, offset)[0]

    def f32(self, offset: int) -> float:
        return _F32.unpack_from(self.data, offset)[0]

    def xyz(self, offset: int) -> tuple[float, float, float]:
        return self.f32(offset), self.f32(offset + 4), self.f32(offset + 8)


def records(frames: Iterable[BDOFrame], geometry: Geometry) -> list[Record]:
    return [Record(frame.context.timestamp, (frame.context.flow, frame.context.flow_generation),
                   frame.message[start:start + geometry.record_length])
            for frame in frames for start in geometry.starts(frame.message)]


def _share(rows: Sequence[Record], test: Callable[[Record], bool]) -> float:
    return sum(map(test, rows)) / len(rows)


def _sample(rows: Sequence[Record]) -> Sequence[Record]:
    """Rows spread over the capture, enough to pick candidate offsets; the checks then read every row."""
    return rows[::max(1, len(rows) // SAMPLE_ROWS)]


def id_offsets(rows: Sequence[Record], *, varying: bool = True) -> list[int]:
    """u32 offsets that look like object IDs: almost never 0, and (with ``varying``) not one constant."""
    sample = _sample(rows)
    return [offset for offset in range(len(rows[0].data) - 3)
            if _share(sample, lambda r: r.u32(offset) != 0) >= MIN_SHARE
            and (not varying or len({r.u32(offset) for r in sample}) > 1)]


def amount_offsets(rows: Sequence[Record]) -> list[int]:
    """f32 offsets that read as a finite, non-negative amount (damage, HP)."""
    sample = _sample(rows)
    return [offset for offset in range(len(rows[0].data) - 3)
            if _share(sample, lambda r: math.isfinite(v := r.f32(offset)) and v >= 0) >= MIN_SHARE]


def _world_position(x: float, y: float, z: float) -> bool:
    """x and z away from the origin and inside the world, a finite height.

    A window a few bytes off the real one reads the same garbage in the hit
    and the kill, so it would match too; it rarely reads as world coordinates.
    """
    return (math.isfinite(y) and MIN_COORDINATE_CM <= abs(x) <= MAX_COORDINATE_CM
            and MIN_COORDINATE_CM <= abs(z) <= MAX_COORDINATE_CM)


def position_offsets(rows: Sequence[Record]) -> list[int]:
    """Offsets of world positions; all zero means no position (15 % of hits at the Orc Camp, the ones without damage)."""
    sample = _sample(rows)
    found = []
    for offset in range(len(rows[0].data) - 11):
        placed = [r for r in sample if r.xyz(offset) != (0.0, 0.0, 0.0)]
        if len(placed) * 2 >= len(sample) and _share(placed, lambda r: _world_position(*r.xyz(offset))) >= MIN_SHARE:
            found.append(offset)
    return found


def overlap(first: int, first_width: int, second: int, second_width: int) -> bool:
    return first < second + second_width and second < first + first_width


@dataclass(frozen=True)
class HitAmounts:
    """A target, HP and damage offset whose HP drops matched the damage."""

    target: int
    hp: int
    damage: int
    agree: int
    disagree: int

    @property
    def agreement(self) -> float:
        return self.agree / (self.agree + self.disagree)


def hit_amounts(rows: Sequence[Record]) -> list[HitAmounts]:
    """Every (target, HP, damage) where consecutive hits on one target lose exactly the damage.

    Each combination has to agree on MIN_HIT_PAIRS drops within the first
    SCREEN_ROWS records (a fight's first hits) before all records are read.
    HP has to change; damage may not (one skill on one dummy).
    """
    amounts = amount_offsets(rows)
    sample = _sample(rows)
    hps = [offset for offset in amounts if len({r.f32(offset) for r in sample}) > 1]
    screen = rows[:SCREEN_ROWS]
    screen_values = {offset: [r.f32(offset) for r in screen] for offset in amounts}
    found = []
    for target in id_offsets(rows):
        screen_keys = [(r.connection, r.u32(target)) for r in screen]
        keys: list[object] = []
        for hp in hps:
            if overlap(target, 4, hp, 4):
                continue
            drops = _hp_drops(screen_keys, screen_values[hp])
            if len(drops) < MIN_HIT_PAIRS:
                continue
            damages = [d for d in amounts if not overlap(d, 4, hp, 4) and not overlap(d, 4, target, 4)
                       and _agrees(drops, screen_values[d])]
            if not damages:
                continue
            keys = keys or [(r.connection, r.u32(target)) for r in rows]
            all_drops = _hp_drops(keys, [r.f32(hp) for r in rows])
            for damage in damages:
                agree, disagree = _drop_agreement(all_drops, [r.f32(damage) for r in rows])
                if agree >= MIN_HIT_PAIRS and agree >= MIN_HIT_AGREEMENT * (agree + disagree):
                    found.append(HitAmounts(target, hp, damage, agree, disagree))
    return found


def _agrees(drops: Sequence[tuple[int, float]], damages: Sequence[float]) -> bool:
    """_drop_agreement passing, stopping at the first disagreement past what the agreement allows."""
    allowed = (1 - MIN_HIT_AGREEMENT) * len(drops)
    agree = disagree = 0
    for index, drop in drops:
        damage = damages[index]
        if not math.isfinite(damage) or damage < 1:
            continue
        if abs(drop - damage) <= 1 + 0.002 * damage:
            agree += 1
        else:
            disagree += 1
            if disagree > allowed:
                return False
    return agree >= MIN_HIT_PAIRS and agree >= MIN_HIT_AGREEMENT * (agree + disagree)


def _hp_drops(keys: Sequence[object], hps: Sequence[float]) -> list[tuple[int, float]]:
    """(record index, HP lost since the target's previous hit).

    Kills and repeated (stale) HP say nothing, and records without an HP
    (0, as in the records that carry no damage) leave the last one in place.
    """
    last: dict[object, float] = {}
    drops = []
    for index, (key, hp) in enumerate(zip(keys, hps)):
        if not math.isfinite(hp) or hp < 1:
            continue
        before = last.get(key)
        last[key] = hp
        if before is not None and before - hp >= 1:
            drops.append((index, before - hp))
    return drops


def _drop_agreement(drops: Sequence[tuple[int, float]], damages: Sequence[float]) -> tuple[int, int]:
    agree = disagree = 0
    for index, drop in drops:
        damage = damages[index]
        if not math.isfinite(damage) or damage < 1:
            continue  # Misses say nothing.
        if abs(drop - damage) <= 1 + 0.002 * damage:
            agree += 1
        else:
            disagree += 1
    return agree, disagree


def lethal_hits(rows: Sequence[Record], amounts: HitAmounts) -> dict[tuple[Connection, int], Record]:
    """The lethal hit on each target (HP 0 after at least 1 damage), the last one when repeated."""
    lethal = {}
    for row in rows:
        damage, hp = row.f32(amounts.damage), row.f32(amounts.hp)
        if math.isfinite(damage) and math.isfinite(hp) and hp <= 0 and damage >= 1:
            lethal[(row.connection, row.u32(amounts.target))] = row
    return lethal


def kill_pairs(lethal: dict[tuple[Connection, int], Record], kills: Sequence[Record],
               victim: int) -> list[tuple[Record, Record]]:
    """(lethal hit, kill) where the kill names the hit's target within the kill window."""
    by_victim: dict[tuple[Connection, int], list[Record]] = defaultdict(list)
    for kill in kills:
        by_victim[(kill.connection, kill.u32(victim))].append(kill)
    pairs = []
    for key, hit in lethal.items():
        match = next((k for k in by_victim.get(key, ()) if abs(k.at - hit.at) <= KILL_WINDOW_SECONDS), None)
        if match is not None:
            pairs.append((hit, match))
    return pairs


def same_id_share(pairs: Sequence[tuple[Record, Record]], hit_offset: int, kill_offset: int) -> float:
    return sum(hit.u32(hit_offset) == kill.u32(kill_offset) for hit, kill in pairs) / len(pairs)


def near_share(pairs: Sequence[tuple[Record, Record]], hit_offset: int, kill_offset: int) -> float:
    """Share of pairs whose kill position lies within MAX_KILL_DISTANCE_CM of the lethal hit's target position."""
    def near(hit: Record, kill: Record) -> bool:
        return math.dist(hit.xyz(hit_offset), kill.xyz(kill_offset)) <= MAX_KILL_DISTANCE_CM
    return sum(near(hit, kill) for hit, kill in pairs) / len(pairs)


def spawn_links(spawns: Sequence[Record], object_offset: int,
                later: Iterable[tuple[Connection, int, float]]) -> dict[tuple[Connection, int], Record]:
    """Spawn records whose object is hit or killed later on the same connection, by object."""
    spawned: dict[tuple[Connection, int], Record] = {}
    for spawn in spawns:
        spawned.setdefault((spawn.connection, spawn.u32(object_offset)), spawn)
    linked = {}
    for connection, object_id, at in later:
        first = spawned.get((connection, object_id))
        if first is not None and first.at <= at:
            linked[(connection, object_id)] = first
    return linked
