"""Immutable class resource change observations."""
from dataclasses import asdict, dataclass
from typing import Literal

from .._tracked_capture import CaptureHealth
from ..events import Flow


class ResourceTrackingError(RuntimeError):
    """The resource layout no longer fits the traffic; stop using readings."""


@dataclass(frozen=True)
class ResourceReading:
    """One class resource change (MP or the class's own kind) the server reported.

    ``change`` is signed: skills cost resource, regeneration and potions restore
    it. ``current`` already includes regeneration since the last message.
    The server also sends nearby mounts' stat in this shape (another
    ``object_id``, a much larger ``maximum``); ``is_own`` marks the player
    once its ID is known.
    """

    object_id: int
    maximum: int
    change: int
    current: int
    observed_at: float
    flow: Flow
    connection_epoch: int
    is_own: bool = False

    @property
    def percentage(self) -> float:
        return 100 * self.current / self.maximum

    def to_dict(self) -> dict[str, object]:
        return {**asdict(self), "schema_version": 1, "event_type": "resource_reading",
                "object_id": f"0x{self.object_id:08X}", "percentage": self.percentage}


@dataclass(frozen=True)
class ResourceStatus:
    """The player's latest resource; an invalid layout hides it.

    ``waiting`` until a reading of the player is known. ``own_object_id`` is
    None until learned (or given) and again after its connection resets.
    """

    status: Literal["waiting", "tracking", "invalid"]
    reading: ResourceReading | None
    reason: str | None = None
    own_object_id: int | None = None

    def to_dict(self) -> dict[str, object]:
        own = None if self.own_object_id is None else f"0x{self.own_object_id:08X}"
        return {"status": self.status, "reading": self.reading.to_dict() if self.reading else None,
                "reason": self.reason, "own_object_id": own}


ResourceCaptureHealth = CaptureHealth
