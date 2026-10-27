"""Find the class resource and position layouts in a capture without trusting previous offsets.

A patch can move the opcodes, change the message lengths and reorder the
fields (the first patch after client 3464 did all three), so every plain
fixed-length message is searched for fields that pass checks random bytes
fail:

- Resource: per object, the next current value often equals the previous
  one plus the change; the maximum stays the same and the current value
  reaches it; changes go both ways (skill costs, regeneration).
- Position: every message holds a world position and a unit facing vector,
  and the player's own track (the object ID the resource message names)
  moves without jumps.

The two meet in the player's own object ID: a resource candidate counts
only when its player also moves in position messages. Callers choose
between what passes with their previous layout.
"""
from collections import Counter, defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
import math
import struct

from ._layout_relocation import FixedLengthCollector, LayoutRelocationError
from ._own_object import PLAYER_OBJECT_ID_LIMIT
from ._protocol import BDOFrame
from .position._tracker import FACING_LENGTH_TOLERANCE, MAX_WORLD_COORDINATE
from .profiles import PositionSource, ResourceProfileLayout

FIRST_FIELD_OFFSET = 5           # after the u16 length, u8 flag and u16 opcode
MIN_DISCOVERY_LENGTH = 21        # the smallest resource message the profile accepts
MAX_DISCOVERY_LENGTH = 512       # both were under 100 bytes on client 3464 and after
MIN_RESOURCE_READINGS = 3
MIN_POSITION_READINGS = 10       # own messages per position source
MAX_RESOURCE_OBJECTS = 64        # the player and nearby mounts
MIN_CHANGE_MATCHES = 2
MIN_CHANGE_MATCH_SHARE = 0.25    # 52 to 62 % in real captures; the action message's coincidences 5 %
MIN_COORDINATE_CM = 100.0        # world x and z are never this near the origin
MAX_SPEED_CM_PER_S = 5000.0      # faster than any mount
MIN_STEP_SECONDS = 0.05
MAX_JUMP_SHARE = 0.05            # a dash or teleport; a wrong ID offset mixes objects far more often

_U32 = struct.Struct("<I")
_I32 = struct.Struct("<i")
_XYZ = struct.Struct("<3f")


@dataclass(frozen=True)
class ResourceMatch:
    layout: ResourceProfileLayout
    own_object_id: int
    readings: int


@dataclass(frozen=True)
class PositionMatch:
    source: PositionSource
    readings: int
    own_readings: int


def discovery_collector() -> FixedLengthCollector:
    return FixedLengthCollector(range(MIN_DISCOVERY_LENGTH, MAX_DISCOVERY_LENGTH + 1))


def _u32(message: bytes, offset: int) -> int:
    return _U32.unpack_from(message, offset)[0]


def _i32(message: bytes, offset: int) -> int:
    return _I32.unpack_from(message, offset)[0]


def _apart(*offsets: int) -> bool:
    """Four-byte fields at these offsets do not overlap."""
    ordered = sorted(offsets)
    return all(b - a >= 4 for a, b in zip(ordered, ordered[1:]))


def _offsets(length: int, width: int) -> range:
    return range(FIRST_FIELD_OFFSET, length - width + 1)


# Resource -------------------------------------------------------------------

def _resource_id_offsets(messages: Sequence[bytes]) -> Iterator[int]:
    for offset in _offsets(len(messages[0]), 4):
        values = [_u32(m, offset) for m in messages]
        if (all(values) and any(v < PLAYER_OBJECT_ID_LIMIT for v in values)
                and len(set(values)) <= MAX_RESOURCE_OBJECTS):
            yield offset


def _consecutive(messages: Sequence[bytes], id_offset: int) -> list[tuple[bytes, bytes]]:
    """Pairs of one object's messages in order."""
    last: dict[int, bytes] = {}
    pairs = []
    for message in messages:
        object_id = _u32(message, id_offset)
        if object_id in last:
            pairs.append((last[object_id], message))
        last[object_id] = message
    return pairs


def _change_fields(pairs: list[tuple[bytes, bytes]], id_offset: int) -> Iterator[tuple[int, int]]:
    """(current, change) offsets where the next current is the previous one plus the change often enough."""
    length = len(pairs[0][0])
    matches: Counter[tuple[int, int]] = Counter()
    for previous, message in pairs:
        by_step: dict[int, list[int]] = defaultdict(list)
        for current in _offsets(length, 4):
            by_step[_u32(message, current) - _u32(previous, current)].append(current)
        for change in _offsets(length, 4):
            step = _i32(message, change)
            if step:
                for current in by_step.get(step, ()):
                    matches[current, change] += 1
    needed = max(MIN_CHANGE_MATCHES, MIN_CHANGE_MATCH_SHARE * len(pairs))
    for (current, change), count in matches.items():
        if count >= needed and _apart(id_offset, current, change):
            yield current, change


def _maximum_fits(messages: Sequence[bytes], maximum: int, current: int, change: int, id_offset: int) -> bool:
    """The maximum is non-zero, the same per object, never below the current value or the change, and reached."""
    per_object: dict[int, int] = {}
    reached = False
    for message in messages:
        top = _u32(message, maximum)
        value = _u32(message, current)
        if top == 0 or value > top or abs(_i32(message, change)) > top:
            return False
        if per_object.setdefault(_u32(message, id_offset), top) != top:
            return False
        reached = reached or value == top
    return reached


def _resource_layouts(opcode: int, messages: Sequence[bytes]) -> Iterator[ResourceProfileLayout]:
    length = len(messages[0])
    for id_offset in _resource_id_offsets(messages):
        pairs = _consecutive(messages, id_offset)
        if len(pairs) < MIN_CHANGE_MATCHES:
            continue
        for current, change in _change_fields(pairs, id_offset):
            steps = [_i32(m, change) for m in messages]
            if not (any(s > 0 for s in steps) and any(s < 0 for s in steps)):
                continue
            for maximum in _offsets(length, 4):
                if (_apart(id_offset, current, change, maximum)
                        and _maximum_fits(messages, maximum, current, change, id_offset)):
                    yield ResourceProfileLayout(opcode, length, maximum, change, id_offset, current)


def _players(messages: Sequence[bytes], layout: ResourceProfileLayout) -> set[int]:
    return {object_id for m in messages if (object_id := _u32(m, layout.object_id_offset)) < PLAYER_OBJECT_ID_LIMIT}


@dataclass(frozen=True)
class Discovery:
    """The resource layout and every position source that carries its player's track."""

    resource: ResourceMatch
    positions: tuple[PositionMatch, ...]


def discover(collector: FixedLengthCollector) -> Discovery:
    """The one resource layout whose player also moves in position messages, with those position sources."""
    scan = _PositionScan(collector)
    resource = _find_resource(collector, scan)
    return Discovery(resource, scan.matches(resource.own_object_id))


def _find_resource(collector: FixedLengthCollector, scan: "_PositionScan") -> ResourceMatch:
    found: dict[ResourceProfileLayout, ResourceMatch] = {}
    for opcode, frames in collector.fixed_length_candidates().items():
        if len(frames) < MIN_RESOURCE_READINGS:
            continue
        messages = [f.message for f in frames]
        for layout in _resource_layouts(opcode, messages):
            for player in _players(messages, layout):
                if scan.matches(player):
                    found.setdefault(layout, ResourceMatch(layout, player, len(frames)))
    if len(found) == 1:
        return next(iter(found.values()))
    if found:
        listed = ", ".join(f"0x{m.layout.opcode:04X} (player 0x{m.own_object_id:08X})" for m in found.values())
        raise LayoutRelocationError(f"Resource: {len(found)} layouts pass the checks ({listed}); capture longer")
    raise LayoutRelocationError(
        f"Resource: no message passes the checks; use skills that cost resource ({MIN_RESOURCE_READINGS}+ times), "
        f"let it refill, and move around for half a minute ({MIN_POSITION_READINGS}+ position messages)")


# Position -------------------------------------------------------------------

def _world(x: float, y: float, z: float) -> bool:
    return (math.isfinite(y) and MIN_COORDINATE_CM <= abs(x) <= MAX_WORLD_COORDINATE
            and MIN_COORDINATE_CM <= abs(z) <= MAX_WORLD_COORDINATE)


def _unit(x: float, y: float, z: float) -> bool:
    return all(map(math.isfinite, (x, y, z))) and abs(math.hypot(x, y, z) - 1.0) <= FACING_LENGTH_TOLERANCE


@dataclass(frozen=True)
class _Shape:
    """The position and facing offsets of one opcode, which do not depend on the player."""

    frames: Sequence[BDOFrame]
    positions: tuple[int, ...]
    facings: tuple[int, ...]


class _PositionScan:
    """Position and facing offsets per opcode, computed once and matched against any own object ID."""

    def __init__(self, collector: FixedLengthCollector) -> None:
        self._shapes: dict[int, _Shape] = {}
        for opcode, frames in collector.fixed_length_candidates().items():
            if len(frames) < MIN_POSITION_READINGS:
                continue
            messages = [f.message for f in frames]
            positions = tuple(o for o in _offsets(len(messages[0]), 12)
                              if all(_world(*_XYZ.unpack_from(m, o)) for m in messages))
            facings = tuple(o for o in _offsets(len(messages[0]), 12)
                            if all(_unit(*_XYZ.unpack_from(m, o)) for m in messages))
            if positions and facings:
                self._shapes[opcode] = _Shape(frames, positions, facings)
        self._cache: dict[int, tuple[PositionMatch, ...]] = {}

    def matches(self, own_object_id: int) -> tuple[PositionMatch, ...]:
        if own_object_id not in self._cache:
            self._cache[own_object_id] = tuple(
                match for opcode, shape in self._shapes.items() for match in _shape_matches(opcode, shape, own_object_id))
        return self._cache[own_object_id]


def _shape_matches(opcode: int, shape: _Shape, own: int) -> Iterator[PositionMatch]:
    messages = [f.message for f in shape.frames]
    length = len(messages[0])
    for id_offset in _offsets(length, 4):
        if not all(_u32(m, id_offset) for m in messages):
            continue
        own_frames = [f for f in shape.frames if _u32(f.message, id_offset) == own]
        if len(own_frames) < MIN_POSITION_READINGS:
            continue
        for position in shape.positions:
            if not (_apart(id_offset, position, position + 4, position + 8) and _smooth(own_frames, position)):
                continue
            for facing in shape.facings:
                if _apart(id_offset, position, position + 4, position + 8, facing, facing + 4, facing + 8):
                    yield PositionMatch(PositionSource(opcode, length, position, id_offset, facing_offset=facing),
                                        len(shape.frames), len(own_frames))


def _smooth(frames: Sequence[BDOFrame], offset: int) -> bool:
    """The track moves, and almost never faster than any mount."""
    points = [_XYZ.unpack_from(f.message, offset) for f in frames]
    if len(set(points)) < 2:
        return False
    jumps = 0
    for (a, pa), (b, pb) in zip(zip(frames, points), zip(frames[1:], points[1:])):
        seconds = max(b.context.timestamp - a.context.timestamp, MIN_STEP_SECONDS)
        jumps += math.dist(pa, pb) / seconds > MAX_SPEED_CM_PER_S
    return jumps <= MAX_JUMP_SHARE * len(frames)
