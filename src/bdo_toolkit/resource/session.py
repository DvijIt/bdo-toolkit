"""Public single-use class resource capture with explicit profile authority."""
from pathlib import Path

from .._capture_options import LiveCaptureOptions
from .._own_object import OwnObjectTracker
from .._tracked_capture import TrackedLiveSession
from ..profiles import OpcodeProfile
from ._tracker import ResourceTracker, profile_layout
from .models import ResourceReading, ResourceStatus


class LiveResourceSession(TrackedLiveSession[ResourceReading, ResourceStatus]):
    """Class resource changes (MP or the class's own kind) from an explicit profile layout.

    The server sends one per resource-costing skill, potion and regeneration tick;
    a full bar sends none. ``status`` follows the player once its ID is
    known (given as ``object_id`` or learned from the profile's
    ``own_object`` sources). ``own_only`` drops nearby mounts' stat.
    Queue loss and native drops fail closed like every capture session.
    """

    def __init__(self, *, profile: OpcodeProfile,
                 live_options: LiveCaptureOptions | None = None,
                 capture_seconds: float | None = None,
                 save_pcap: str | Path | None = None,
                 object_id: int | None = None,
                 own_only: bool = False) -> None:
        layout = profile_layout(profile)
        own = OwnObjectTracker(profile.own_object, object_id=object_id)
        super().__init__(label="Resource", live_options=live_options, capture_seconds=capture_seconds,
                         save_pcap=save_pcap,
                         make_tracker=lambda publish: ResourceTracker(layout, publish, own, own_only=own_only))
