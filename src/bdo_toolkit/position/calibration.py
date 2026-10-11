"""Re-find the position opcodes after a patch and update a profile explicitly.

Each source keeps its message length and offsets; only its opcode is
searched. A candidate is accepted when every message holds a world
position and a unit facing vector, which other messages of that length do
not. Capture half a minute or more of walking or riding (with other players
nearby is fine). Nothing is written until ``update_position_profile``.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import math
import struct
from pathlib import Path

from .._capture_options import LiveCaptureOptions
from .._layout_relocation import (FixedLengthCollector, LayoutProfileUpdate, LayoutRelocationError, collect_live,
                                  collect_replay, require_clean, single, update_profile_sections)
from .._protocol import DEFAULT_SERVER_PORTS, BDOFrame
from .._tracked_capture import CaptureHealth
from ..profiles import OpcodeProfile, PositionProfileLayout, PositionSource
from ._tracker import FACING_LENGTH_TOLERANCE, MAX_WORLD_COORDINATE, profile_layout

MIN_POSITION_READINGS = 10

_XYZ = struct.Struct("<3f")


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


def _passes(frames: list[BDOFrame], source: PositionSource) -> bool:
    """Every message holds what the position decoder accepts, facing included."""
    if len(frames) < MIN_POSITION_READINGS or source.facing_offset is None:
        return False
    for frame in frames:
        if not all(math.isfinite(v) and abs(v) <= MAX_WORLD_COORDINATE
                   for v in _XYZ.unpack_from(frame.message, source.position_offset)):
            return False
        facing = _XYZ.unpack_from(frame.message, source.facing_offset)
        if not all(math.isfinite(v) for v in facing) or abs(math.hypot(*facing) - 1.0) > FACING_LENGTH_TOLERANCE:
            return False
    return True


def _relocate(collector: FixedLengthCollector, previous: PositionProfileLayout,
              health: CaptureHealth) -> PositionCalibrationResult:
    require_clean(health)
    sources: list[PositionSource] = []
    readings: list[int] = []
    for source in previous.sources:
        if source.facing_offset is None:
            raise LayoutRelocationError(f"Position source 0x{source.opcode:04X} has no facing offset, "
                                        "which relocation needs; update it by hand")
        candidates = {opcode: frames for opcode, frames in collector.candidates(source.message_length).items()
                      if opcode not in {s.opcode for s in sources}}
        passing = [opcode for opcode, frames in candidates.items() if _passes(frames, replace(source, opcode=opcode))]
        opcode = single(f"Position source of {source.message_length} bytes", passing, len(candidates),
                        f"move around for longer ({MIN_POSITION_READINGS}+ messages per source), "
                        "or the message layout changed")
        sources.append(replace(source, opcode=opcode))
        readings.append(len(candidates[opcode]))
    return PositionCalibrationResult(replace(previous, sources=tuple(sources)), previous, tuple(readings), health,
                                     datetime.now(UTC).isoformat())


def _collector(previous: PositionProfileLayout) -> FixedLengthCollector:
    return FixedLengthCollector(source.message_length for source in previous.sources)


def relocate_position_replay(path: str | Path, *, profile: OpcodeProfile,
                             ports: Iterable[int] = DEFAULT_SERVER_PORTS) -> PositionCalibrationResult:
    """Re-find the profile's position opcodes in a saved capture; never writes a file."""
    previous = profile_layout(profile)
    collector = _collector(previous)
    health = collect_replay(path, collector, ports=ports, label="Position calibration")
    return _relocate(collector, previous, health)


def relocate_position_live(*, profile: OpcodeProfile, capture_seconds: float,
                           live_options: LiveCaptureOptions | None = None,
                           save_pcap: str | Path | None = None) -> PositionCalibrationResult:
    """Capture for ``capture_seconds`` while moving, then re-find the position opcodes; never writes a file."""
    previous = profile_layout(profile)
    collector = _collector(previous)
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
