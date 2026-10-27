"""Find the class resource layout again after a patch and update a profile explicitly.

A patch can move the opcode, change the message length and reorder the
fields, so relocation finds all three from the capture itself
(``_telemetry_discovery``): per object, the next current value often equals
the previous one plus the change, the maximum stays the same and is
reached, and the player it names also moves in position messages. An
``own_object`` source that is the resource message moves with it. Capture
half a minute of moving with three or more skills that cost resource, and
let it refill. Nothing is written until ``update_resource_profile``.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from .._capture_options import LiveCaptureOptions
from .._layout_relocation import (FixedLengthCollector, LayoutProfileUpdate, LayoutRelocationError, collect_live,
                                  collect_replay, require_clean, update_profile_sections)
from .._protocol import DEFAULT_SERVER_PORTS
from .._telemetry_discovery import discover, discovery_collector
from .._tracked_capture import CaptureHealth
from ..profiles import OpcodeProfile, OwnObjectProfileLayout, ResourceProfileLayout
from ._tracker import profile_layout


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


def _moved_own_object(own: OwnObjectProfileLayout | None, previous: ResourceProfileLayout,
                      layout: ResourceProfileLayout) -> OwnObjectProfileLayout | None:
    """The own_object layout with every source that is the resource message moved to its new layout."""
    if own is None:
        return None
    sources = []
    for source in own.sources:
        if (source.opcode, source.message_length, source.object_id_offset) != (
                previous.opcode, previous.message_length, previous.object_id_offset):
            raise LayoutRelocationError(f"Own-object source 0x{source.opcode:04X} is not the resource message; "
                                        "relocation cannot check it, update it by hand")
        sources.append(replace(source, opcode=layout.opcode, message_length=layout.message_length,
                               object_id_offset=layout.object_id_offset))
    return replace(own, sources=tuple(sources))


def _relocate(collector: FixedLengthCollector, previous: ResourceProfileLayout,
              own: OwnObjectProfileLayout | None, health: CaptureHealth) -> ResourceCalibrationResult:
    require_clean(health)
    found = discover(collector).resource
    layout = replace(found.layout, flag=previous.flag, encoding=previous.encoding)
    return ResourceCalibrationResult(layout, _moved_own_object(own, previous, layout), previous, own,
                                     found.readings, health, datetime.now(UTC).isoformat())


def relocate_resource_replay(path: str | Path, *, profile: OpcodeProfile,
                             ports: Iterable[int] = DEFAULT_SERVER_PORTS) -> ResourceCalibrationResult:
    """Find the profile's resource layout again in a saved capture; never writes a file."""
    previous = profile_layout(profile)
    collector = discovery_collector()
    health = collect_replay(path, collector, ports=ports, label="Resource calibration")
    return _relocate(collector, previous, profile.own_object, health)


def relocate_resource_live(*, profile: OpcodeProfile, capture_seconds: float,
                           live_options: LiveCaptureOptions | None = None,
                           save_pcap: str | Path | None = None) -> ResourceCalibrationResult:
    """Capture for ``capture_seconds`` while moving and using skills, then find the layout again; never writes."""
    previous = profile_layout(profile)
    collector = discovery_collector()
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
