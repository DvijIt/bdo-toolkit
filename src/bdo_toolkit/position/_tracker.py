"""World position decoder for live capture and replay."""
import math
import struct
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import replace
from typing import NoReturn

from .._own_object import OwnObjectError, OwnObjectTracker
from .._profile_runtime import validate_runtime_profile
from .._protocol import BDOFrame, FlowKey
from ..events import Flow
from ..profiles import OpcodeProfile, PositionProfileLayout, PositionSource, ProfileError
from .models import PositionReading, PositionStatus, PositionTrackingError

# Far beyond any map edge (the world spans a few million cm); larger values
# mean the offsets no longer point at a position.
MAX_WORLD_COORDINATE = 10_000_000.0
# Latest position kept per object; a crowded town shows a few hundred.
MAX_TRACKED_OBJECTS = 4096
# The facing vector had length 1 within 0.001 in 166 messages of client 3464;
# a longer or shorter one means the offset no longer points at it.
FACING_LENGTH_TOLERANCE = 0.01

_XYZ = struct.Struct("<3f")
_OBJECT_ID = struct.Struct("<I")


def profile_layout(profile: OpcodeProfile) -> PositionProfileLayout:
    if not isinstance(profile, OpcodeProfile):
        raise TypeError("profile must be a loaded OpcodeProfile")
    validate_runtime_profile(profile)
    if profile.position is None:
        raise ProfileError("Profile has no position layout; add one for this patch")
    return profile.position


class PositionTracker:
    """Decodes every nearby object's position; marks the player's own.

    Every layout source feeds one latest position per object; a source
    without a facing keeps the object's last known facing. Positions only
    read the player's ID from ``own``: they are sent for every nearby mover,
    so they cannot reveal it. Positions do not accumulate, so TCP gaps and
    reconnects only miss readings. A source message with another shape,
    values that are no position, or a facing that is no unit vector
    withdraw authority, as do the errors of ``own``.
    """

    def __init__(self, layout: PositionProfileLayout, on_reading: Callable[[PositionReading], None],
                 own: OwnObjectTracker, *, own_only: bool = False):
        if type(own_only) is not bool:
            raise TypeError("own_only must be a bool")
        if own_only and not own.can_identify:
            raise ProfileError("own_only needs object_id or an own_object layout in the profile")
        self.layout = layout
        self._sources = {source.opcode: source for source in layout.sources}
        self._publish = on_reading
        self._own = own
        self._own_only = own_only
        self._latest: OrderedDict[int, PositionReading] = OrderedDict()
        self.reading: PositionReading | None = None
        self.reason: str | None = None
        own.subscribe(self._own_changed)

    def snapshot(self) -> PositionStatus:
        return PositionStatus("invalid" if self.reason else "tracking" if self.reading else "waiting",
                              self.reading, self.reason, self._own.own_object_id)

    def failed_snapshot(self, reason: str) -> PositionStatus:
        return PositionStatus("invalid", None, reason, self._own.own_object_id)

    def invalidate(self, reason: str) -> NoReturn:
        self.reason = self.reason or reason
        self.reading = None
        raise PositionTrackingError(self.reason)

    def observe(self, frame: BDOFrame) -> None:
        if self.reason:
            raise PositionTrackingError(self.reason)
        try:
            self._own.observe(frame)
        except OwnObjectError as exc:
            self.invalidate(str(exc))
        source = self._sources.get(frame.opcode)
        if source is not None:
            self._observe_position(frame, source)

    def close_flow(self, flow: FlowKey) -> None:
        self._own.close_flow(flow)

    def _observe_position(self, frame: BDOFrame, source: PositionSource) -> None:
        if (frame.flag != source.flag or frame.length != source.message_length
                or len(frame.message) != source.message_length):
            self.invalidate(f"Position message 0x{frame.opcode:04X} shape changed; "
                            "update this patch's position layout")
        x, y, z = _XYZ.unpack_from(frame.message, source.position_offset)
        (object_id,) = _OBJECT_ID.unpack_from(frame.message, source.object_id_offset)
        if not all(math.isfinite(v) and abs(v) <= MAX_WORLD_COORDINATE for v in (x, y, z)):
            self.invalidate("Position fields failed validation; review the position layout")
        if not math.isfinite(frame.context.timestamp):
            self.invalidate("Position message has no valid timestamp")
        facing_x, facing_z = self._facing(frame, source, object_id)
        is_own = self._own.is_own(object_id)
        flow = frame.context.flow
        reading = PositionReading(object_id, x, y, z, frame.context.timestamp,
                                  Flow(flow.source_ip, flow.source_port, flow.destination_ip,
                                       flow.destination_port),
                                  frame.context.flow_generation, is_own, facing_x, facing_z)
        old = self._remember(reading)
        if is_own:
            self.reading = reading
        # Repeats refresh last-seen state but do not consume the event queue.
        if old is not None and (old.x, old.y, old.z, old.facing_x, old.facing_z, old.is_own) == (
                x, y, z, facing_x, facing_z, is_own):
            return
        if is_own or not self._own_only:
            self._publish(reading)

    def _facing(self, frame: BDOFrame, source: PositionSource, object_id: int) -> tuple[float | None, float | None]:
        if source.facing_offset is None:
            last = self._latest.get(object_id)
            return (None, None) if last is None else (last.facing_x, last.facing_z)
        fx, fy, fz = _XYZ.unpack_from(frame.message, source.facing_offset)
        if not all(math.isfinite(v) for v in (fx, fy, fz))                 or abs(math.sqrt(fx * fx + fy * fy + fz * fz) - 1.0) > FACING_LENGTH_TOLERANCE:
            self.invalidate(f"Facing in 0x{frame.opcode:04X} is no unit vector; review the position layout")
        return fx, fz

    def _own_changed(self, own_object_id: int | None) -> None:
        self.reading = None
        if own_object_id is None or own_object_id not in self._latest:
            return
        # The player moved before its ID was known: report that position as its own now.
        self.reading = replace(self._latest[own_object_id], is_own=True)
        self._latest[own_object_id] = self.reading
        self._publish(self.reading)

    def _remember(self, reading: PositionReading) -> PositionReading | None:
        old = self._latest.pop(reading.object_id, None)
        self._latest[reading.object_id] = reading
        if len(self._latest) > MAX_TRACKED_OBJECTS:
            self._latest.popitem(last=False)
        return old
