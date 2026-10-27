"""Find the position layout again after a patch and update a profile explicitly.

A patch can move the opcodes, change the message lengths and reorder the
fields, so relocation finds all three from the capture itself
(``_telemetry_discovery``): a source holds a world position and a unit
facing vector in every message, and the player's own track, named by the
class resource message, moves smoothly. The action message passes too, so
each previous source takes the candidate nearest its message length, and
the offsets nearest its own when several pass. Capture half a minute or
more of walking or riding (other players nearby are fine) with three or
more skills that cost resource, and let it refill. Nothing is written until
``update_position_profile``.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .._capture_options import LiveCaptureOptions
from .._layout_relocation import (FixedLengthCollector, LayoutProfileUpdate, LayoutRelocationError, collect_live,
                                  collect_replay, require_clean, update_profile_sections)
from .._protocol import DEFAULT_SERVER_PORTS
from .._telemetry_discovery import PositionMatch, discover, discovery_collector
from .._tracked_capture import CaptureHealth
from ..profiles import OpcodeProfile, PositionProfileLayout, PositionSource
from ._tracker import profile_layout


@dataclass(frozen=True)
class PositionCalibrationResult:
    """A relocated position layout from a clean capture; ``readings`` per source, in layout order."""

    layout: PositionProfileLayout
    previous: PositionProfileLayout
    readings: tuple[int, ...]
    health: CaptureHealth
    completed_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.layout, PositionProfileLayout) or not isinstance(self.previous, PositionProfileLayout):
            raise TypeError("layout and previous must be PositionProfileLayout")
        self.layout.__post_init__()
        require_clean(self.health)

    @property
    def changed(self) -> bool:
        return self.layout != self.previous

    def to_dict(self) -> dict[str, object]:
        return {"layout": self.layout.to_dict(), "previous": self.previous.to_dict(), "changed": self.changed,
                "readings": list(self.readings), "health": self.health.to_dict(), "completed_at": self.completed_at}


def _nearest_opcode(source: PositionSource, by_opcode: dict[int, list[PositionMatch]]) -> int:
    """The candidate opcode whose message length is nearest the previous source's."""
    def distance(opcode: int) -> int:
        return abs(by_opcode[opcode][0].source.message_length - source.message_length)

    ranked = sorted(by_opcode, key=distance)
    if len(ranked) > 1 and distance(ranked[0]) == distance(ranked[1]):
        raise LayoutRelocationError(
            f"Position source 0x{source.opcode:04X} ({source.message_length} bytes): 0x{ranked[0]:04X} and "
            f"0x{ranked[1]:04X} are equally near its length; update the position layout by hand")
    return ranked[0]


def _nearest_offsets(source: PositionSource, matches: Sequence[PositionMatch]) -> PositionMatch:
    """Of one opcode's passing offsets, the ones nearest the previous source's."""
    def distance(match: PositionMatch) -> int:
        found, facing = match.source, source.facing_offset
        return (abs(found.position_offset - source.position_offset)
                + abs(found.object_id_offset - source.object_id_offset)
                + (0 if facing is None or found.facing_offset is None else abs(found.facing_offset - facing)))

    ranked = sorted(matches, key=distance)
    if len(ranked) > 1 and distance(ranked[0]) == distance(ranked[1]):
        raise LayoutRelocationError(f"Position 0x{ranked[0].source.opcode:04X}: two offset sets are equally near "
                                    "the previous ones; update the position layout by hand")
    return ranked[0]


def _assign(previous: PositionProfileLayout, matches: Sequence[PositionMatch]) -> tuple[PositionMatch, ...]:
    """One passing source per previous source, in layout order."""
    by_opcode: dict[int, list[PositionMatch]] = defaultdict(list)
    for match in matches:
        by_opcode[match.source.opcode].append(match)
    if len(by_opcode) < len(previous.sources):
        raise LayoutRelocationError(
            f"Position: {len(by_opcode)} of {len(previous.sources)} sources carry the player's track; "
            "move around for longer, walking backwards for a few seconds too")
    opcodes = [_nearest_opcode(source, by_opcode) for source in previous.sources]
    if len(set(opcodes)) != len(opcodes):
        raise LayoutRelocationError("Position: two previous sources are nearest the same message; "
                                    "update the position layout by hand")
    return tuple(_nearest_offsets(source, by_opcode[opcode]) for source, opcode in zip(previous.sources, opcodes))


def _relocate(collector: FixedLengthCollector, previous: PositionProfileLayout,
              health: CaptureHealth) -> PositionCalibrationResult:
    require_clean(health)
    chosen = _assign(previous, discover(collector).positions)
    layout = PositionProfileLayout(tuple(match.source for match in chosen), previous.encoding)
    return PositionCalibrationResult(layout, previous, tuple(match.readings for match in chosen), health,
                                     datetime.now(UTC).isoformat())


def relocate_position_replay(path: str | Path, *, profile: OpcodeProfile,
                             ports: Iterable[int] = DEFAULT_SERVER_PORTS) -> PositionCalibrationResult:
    """Find the profile's position sources again in a saved capture; never writes a file."""
    previous = profile_layout(profile)
    collector = discovery_collector()
    health = collect_replay(path, collector, ports=ports, label="Position calibration")
    return _relocate(collector, previous, health)


def relocate_position_live(*, profile: OpcodeProfile, capture_seconds: float,
                           live_options: LiveCaptureOptions | None = None,
                           save_pcap: str | Path | None = None) -> PositionCalibrationResult:
    """Capture for ``capture_seconds`` while moving and using skills, then find the sources again; never writes."""
    previous = profile_layout(profile)
    collector = discovery_collector()
    health = collect_live(collector, capture_seconds=capture_seconds, label="Position calibration",
                          live_options=live_options, save_pcap=save_pcap)
    return _relocate(collector, previous, health)


def update_position_profile(result: PositionCalibrationResult, path: str | Path, *, backup: bool = True,
                            expected_profile_sha256: str | None = None) -> LayoutProfileUpdate:
    """Replace only the position layout of an existing active profile (backup, atomic replace)."""
    if not isinstance(result, PositionCalibrationResult):
        raise TypeError("result must be a PositionCalibrationResult")
    result.__post_init__()
    return update_profile_sections(path, {"position": result.layout}, {"position": result.previous},
                                   backup=backup, expected_profile_sha256=expected_profile_sha256)
