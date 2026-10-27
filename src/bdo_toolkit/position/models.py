"""Immutable world position observations; an object ID is not a player identity."""
import math
from dataclasses import asdict, dataclass
from typing import Literal

from .._tracked_capture import CaptureHealth
from ..events import Flow


class PositionTrackingError(RuntimeError):
    """The position layout no longer fits the traffic; stop using readings."""


@dataclass(frozen=True)
class PositionReading:
    """One nearby object's world position as the server reported it.

    x and z are the ground plane, y is the height, all in the game files'
    centimetres. ``object_id`` is the moving object: the player, another
    player, an NPC or a monster. The player's ID is new on every login and
    stays the same while mounted. ``is_own`` is True once the player's ID is
    known and this is the player; earlier readings of the player say False.
    ``facing_x`` and ``facing_z`` are the facing direction on the ground
    plane (a unit vector), None when the layout has no facing.
    """

    object_id: int
    x: float
    y: float
    z: float
    observed_at: float
    flow: Flow
    connection_epoch: int
    is_own: bool = False
    facing_x: float | None = None
    facing_z: float | None = None

    @property
    def heading(self) -> float | None:
        """Facing in degrees clockwise from north (+z, east is +x), 0 to 360."""
        if self.facing_x is None or self.facing_z is None:
            return None
        return math.degrees(math.atan2(self.facing_x, self.facing_z)) % 360.0

    def to_dict(self) -> dict[str, object]:
        return {**asdict(self), "schema_version": 3, "event_type": "position_reading",
                "object_id": f"0x{self.object_id:08X}", "heading": self.heading}


@dataclass(frozen=True)
class PositionStatus:
    """The player's latest position; an invalid layout hides it.

    ``waiting`` until the player's ID and a position for it are both known.
    ``own_object_id`` is None until learned (or given) and again after the
    connection it was learned on resets.
    """

    status: Literal["waiting", "tracking", "invalid"]
    reading: PositionReading | None
    reason: str | None = None
    own_object_id: int | None = None

    def to_dict(self) -> dict[str, object]:
        own = None if self.own_object_id is None else f"0x{self.own_object_id:08X}"
        return {"status": self.status, "reading": self.reading.to_dict() if self.reading else None,
                "reason": self.reason, "own_object_id": own}


PositionCaptureHealth = CaptureHealth
