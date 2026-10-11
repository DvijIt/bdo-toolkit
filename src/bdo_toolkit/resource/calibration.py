"""Re-find the class resource opcode after a patch and update a profile explicitly.

The resource layout keeps its message length and offsets; only its opcode
is searched. A candidate is accepted when every message is a valid change
(current within a positive maximum that stays the same per object) and one
names a player. An ``own_object`` source that is the resource message moves
with it. Capture half a minute of using skills. Nothing is written until
``update_resource_profile``.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import struct
from pathlib import Path

from .._capture_options import LiveCaptureOptions
from .._layout_relocation import (FixedLengthCollector, LayoutProfileUpdate, LayoutRelocationError, collect_live,
                                  collect_replay, require_clean, single, update_profile_sections)
from .._own_object import PLAYER_OBJECT_ID_LIMIT
from .._protocol import DEFAULT_SERVER_PORTS, BDOFrame
from .._tracked_capture import CaptureHealth
from ..profiles import OpcodeProfile, OwnObjectProfileLayout, ResourceProfileLayout
from ._tracker import profile_layout

MIN_RESOURCE_READINGS = 3

_U32 = struct.Struct("<I")
_I32 = struct.Struct("<i")


@dataclass(frozen=True)
class ResourceCalibrationResult:
    """A relocated resource layout (and own_object, when it follows it) from a clean capture."""

    layout: ResourceProfileLayout
    own_object: OwnObjectProfileLayout | None
    previous: ResourceProfileLayout
    previous_own_object: OwnObjectProfileLayout | None
    readings: int
    health: CaptureHealth
    completed_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.layout, ResourceProfileLayout) or not isinstance(self.previous, ResourceProfileLayout):
            raise TypeError("layout and previous must be ResourceProfileLayout")
        self.layout.__post_init__()
        require_clean(self.health)

    @property
    def changed(self) -> bool:
        return self.layout != self.previous or self.own_object != self.previous_own_object

    def to_dict(self) -> dict[str, object]:
        own = self.own_object.to_dict() if self.own_object else None
        return {"layout": self.layout.to_dict(), "own_object": own, "changed": self.changed,
                "previous": self.previous.to_dict(), "readings": self.readings,
                "health": self.health.to_dict(), "completed_at": self.completed_at}


def _passes(frames: list[BDOFrame], layout: ResourceProfileLayout) -> bool:
    """Every message is a change the resource decoder accepts, and they look like one stat per object."""
    if len(frames) < MIN_RESOURCE_READINGS:
        return False
    maxima: dict[int, set[int]] = defaultdict(set)
    changed = names_player = False
    for frame in frames:
        message = frame.message
        (maximum,) = _U32.unpack_from(message, layout.maximum_offset)
        (change,) = _I32.unpack_from(message, layout.change_offset)
        (object_id,) = _U32.unpack_from(message, layout.object_id_offset)
        (current,) = _U32.unpack_from(message, layout.current_offset)
        if maximum == 0 or current > maximum or abs(change) > maximum:
            return False
        maxima[object_id].add(maximum)
        changed = changed or change != 0
        names_player = names_player or object_id < PLAYER_OBJECT_ID_LIMIT
    return changed and names_player and all(len(values) == 1 for values in maxima.values())


def _moved_own_object(own: OwnObjectProfileLayout | None, previous: ResourceProfileLayout,
                      opcode: int) -> OwnObjectProfileLayout | None:
    """The own_object layout with every source that is the resource message moved to ``opcode``."""
    if own is None:
        return None
    sources = []
    for source in own.sources:
        if (source.opcode, source.message_length, source.object_id_offset) == (
                previous.opcode, previous.message_length, previous.object_id_offset):
            sources.append(replace(source, opcode=opcode))
        else:
            raise LayoutRelocationError(f"Own-object source 0x{source.opcode:04X} is not the resource message; "
                                        "relocation cannot check it, update it by hand")
    return replace(own, sources=tuple(sources))


def _relocate(collector: FixedLengthCollector, previous: ResourceProfileLayout,
              own: OwnObjectProfileLayout | None, health: CaptureHealth) -> ResourceCalibrationResult:
    require_clean(health)
    candidates = collector.candidates(previous.message_length)
    passing = [opcode for opcode, frames in candidates.items() if _passes(frames, replace(previous, opcode=opcode))]
    opcode = single("Resource", passing, len(candidates),
                    f"use skills that cost resource ({MIN_RESOURCE_READINGS}+ times), or the message layout changed")
    return ResourceCalibrationResult(replace(previous, opcode=opcode), _moved_own_object(own, previous, opcode),
                                     previous, own, len(candidates[opcode]), health, datetime.now(UTC).isoformat())


def relocate_resource_replay(path: str | Path, *, profile: OpcodeProfile,
                             ports: Iterable[int] = DEFAULT_SERVER_PORTS) -> ResourceCalibrationResult:
    """Re-find the profile's resource opcode in a saved capture; never writes a file."""
    previous = profile_layout(profile)
    collector = FixedLengthCollector((previous.message_length,))
    health = collect_replay(path, collector, ports=ports, label="Resource calibration")
    return _relocate(collector, previous, profile.own_object, health)


def relocate_resource_live(*, profile: OpcodeProfile, capture_seconds: float,
                           live_options: LiveCaptureOptions | None = None,
                           save_pcap: str | Path | None = None) -> ResourceCalibrationResult:
    """Capture for ``capture_seconds`` while using skills, then re-find the resource opcode; never writes."""
    previous = profile_layout(profile)
    collector = FixedLengthCollector((previous.message_length,))
    health = collect_live(collector, capture_seconds=capture_seconds, label="Resource calibration",
                          live_options=live_options, save_pcap=save_pcap)
    return _relocate(collector, previous, profile.own_object, health)


def update_resource_profile(result: ResourceCalibrationResult, path: str | Path, *, backup: bool = True,
                            expected_profile_sha256: str | None = None) -> LayoutProfileUpdate:
    """Replace only the resource and own_object layouts of an existing active profile (backup, atomic replace)."""
    if not isinstance(result, ResourceCalibrationResult):
        raise TypeError("result must be a ResourceCalibrationResult")
    result.__post_init__()
    return update_profile_sections(path, {"resource": result.layout, "own_object": result.own_object},
                                   {"resource": result.previous, "own_object": result.previous_own_object},
                                   backup=backup, expected_profile_sha256=expected_profile_sha256)
