"""Class resource change decoder for live capture and replay."""
import math
import struct
from collections.abc import Callable
from typing import NoReturn

from .._own_object import OwnObjectError, OwnObjectTracker
from .._profile_runtime import validate_runtime_profile
from .._protocol import BDOFrame, FlowKey
from ..events import Flow
from ..profiles import OpcodeProfile, ProfileError, ResourceProfileLayout
from .models import ResourceReading, ResourceStatus, ResourceTrackingError

_U32 = struct.Struct("<I")
_I32 = struct.Struct("<i")


def profile_layout(profile: OpcodeProfile) -> ResourceProfileLayout:
    if not isinstance(profile, OpcodeProfile):
        raise TypeError("profile must be a loaded OpcodeProfile")
    validate_runtime_profile(profile)
    if profile.resource is None:
        raise ProfileError("Profile has no resource layout; add one for this patch")
    return profile.resource


class ResourceTracker:
    """Decodes every class resource change; marks the player's own.

    The own-ID sources run first, so a resource message that reveals the player
    is already marked as its own. A message with another shape, or a current
    value above the maximum, withdraws authority.
    """

    def __init__(self, layout: ResourceProfileLayout, on_reading: Callable[[ResourceReading], None],
                 own: OwnObjectTracker, *, own_only: bool = False):
        if type(own_only) is not bool:
            raise TypeError("own_only must be a bool")
        if own_only and not own.can_identify:
            raise ProfileError("own_only needs object_id or an own_object layout in the profile")
        self.layout = layout
        self._publish = on_reading
        self._own = own
        self._own_only = own_only
        self.reading: ResourceReading | None = None
        self.reason: str | None = None
        own.subscribe(self._own_changed)

    def snapshot(self) -> ResourceStatus:
        return ResourceStatus("invalid" if self.reason else "tracking" if self.reading else "waiting",
                        self.reading, self.reason, self._own.own_object_id)

    def failed_snapshot(self, reason: str) -> ResourceStatus:
        return ResourceStatus("invalid", None, reason, self._own.own_object_id)

    def invalidate(self, reason: str) -> NoReturn:
        self.reason = self.reason or reason
        self.reading = None
        raise ResourceTrackingError(self.reason)

    def observe(self, frame: BDOFrame) -> None:
        if self.reason:
            raise ResourceTrackingError(self.reason)
        try:
            self._own.observe(frame)
        except OwnObjectError as exc:
            self.invalidate(str(exc))
        if frame.opcode == self.layout.opcode:
            self._observe_resource(frame)

    def close_flow(self, flow: FlowKey) -> None:
        self._own.close_flow(flow)

    def _observe_resource(self, frame: BDOFrame) -> None:
        layout = self.layout
        if (frame.flag != layout.flag or frame.length != layout.message_length
                or len(frame.message) != layout.message_length):
            self.invalidate("Resource message shape changed; update this patch's resource layout")
        message = frame.message
        (maximum,) = _U32.unpack_from(message, layout.maximum_offset)
        (change,) = _I32.unpack_from(message, layout.change_offset)
        (object_id,) = _U32.unpack_from(message, layout.object_id_offset)
        (current,) = _U32.unpack_from(message, layout.current_offset)
        if maximum == 0 or current > maximum or not math.isfinite(frame.context.timestamp):
            self.invalidate("Resource fields failed validation; review the resource layout")
        is_own = self._own.is_own(object_id)
        flow = frame.context.flow
        reading = ResourceReading(object_id, maximum, change, current, frame.context.timestamp,
                            Flow(flow.source_ip, flow.source_port, flow.destination_ip, flow.destination_port),
                            frame.context.flow_generation, is_own)
        if is_own:
            self.reading = reading
        if is_own or not self._own_only:
            self._publish(reading)

    def _own_changed(self, own_object_id: int | None) -> None:
        # A reading of another (or no) player is no longer the player's resource.
        if self.reading is not None and self.reading.object_id != own_object_id:
            self.reading = None
