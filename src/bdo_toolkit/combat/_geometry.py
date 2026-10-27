"""Record list geometry found again from message lengths and the count byte.

A record list message is ``header_length + record_length * count`` bytes with
the uint8 count at ``count_offset``. Two messages of one opcode with
different counts fix all three; when every message holds the same count,
only the previous geometry (or the previous header with the record length the
lengths imply) is offered, for the field checks to accept or reject.
"""
from collections.abc import Sequence
from dataclasses import dataclass

from ..profiles import CombatHitLayout, CombatKillLayout, CombatSpawnLayout

# The bounds profiles._check_record_list enforces.
MIN_HEADER_LENGTH = 6
MAX_HEADER_LENGTH = 4096
MAX_RECORD_LENGTH = 16384
FIRST_COUNT_OFFSET = 5  # after the u16 length, u8 flag and u16 opcode

type RecordLayout = CombatSpawnLayout | CombatHitLayout | CombatKillLayout


@dataclass(frozen=True)
class Geometry:
    header_length: int
    count_offset: int
    record_length: int

    @classmethod
    def of(cls, layout: RecordLayout) -> "Geometry":
        return cls(layout.header_length, layout.count_offset, layout.record_length)

    def fits(self, message: bytes) -> bool:
        body = len(message) - self.header_length
        return (body > 0 and body % self.record_length == 0
                and message[self.count_offset] == body // self.record_length)

    def starts(self, message: bytes) -> range:
        return range(self.header_length, len(message), self.record_length)


def solved_geometries(messages: Sequence[bytes]) -> tuple[Geometry, ...]:
    """Every geometry that two different record counts fix and all messages fit."""
    shortest = min(len(message) for message in messages)
    found = []
    for count_offset in range(FIRST_COUNT_OFFSET, min(shortest, MAX_HEADER_LENGTH)):
        geometry = _solve(messages, count_offset)
        if geometry is not None:
            found.append(geometry)
    return tuple(found)


def fallback_geometries(messages: Sequence[bytes], previous: Geometry) -> tuple[Geometry, ...]:
    """For messages that never show two record counts: the previous geometry, or its header with a new record length."""
    if all(previous.fits(message) for message in messages):
        return (previous,)
    lengths = {len(message) for message in messages}
    if previous.count_offset >= min(lengths):
        return ()
    counts = {message[previous.count_offset] for message in messages}
    if len(lengths) != 1 or len(counts) != 1:
        return ()
    (length,), (count,) = lengths, counts
    body = length - previous.header_length
    if count == 0 or body <= 0 or body % count or body // count > MAX_RECORD_LENGTH:
        return ()
    return (Geometry(previous.header_length, previous.count_offset, body // count),)


def _solve(messages: Sequence[bytes], count_offset: int) -> Geometry | None:
    first = messages[0]
    first_count = first[count_offset]
    other = next((message for message in messages if message[count_offset] != first_count), None)
    if first_count == 0 or other is None:
        return None
    length_change = len(other) - len(first)
    count_change = other[count_offset] - first_count
    if length_change % count_change:
        return None
    record_length = length_change // count_change
    if not 1 <= record_length <= MAX_RECORD_LENGTH:
        return None
    header_length = len(first) - record_length * first_count
    if not MIN_HEADER_LENGTH <= header_length <= MAX_HEADER_LENGTH or count_offset >= header_length:
        return None
    geometry = Geometry(header_length, count_offset, record_length)
    return geometry if all(geometry.fits(message) for message in messages) else None
