"""Public single-use world position capture with explicit profile authority."""
from pathlib import Path

from .._capture_options import LiveCaptureOptions
from .._own_object import OwnObjectTracker
from .._tracked_capture import TrackedLiveSession
from ..profiles import OpcodeProfile
from ._tracker import PositionTracker, profile_layout
from .models import PositionReading, PositionStatus


class LivePositionSession(TrackedLiveSession[PositionReading, PositionStatus]):
    """World positions of nearby moving objects from an explicit profile layout.

    The server sends a position when an object's movement changes (0.15 s to
    several seconds apart); standing still sends none, so the last reading
    stays current. ``status`` follows the player once its ID is known: pass
    ``object_id``, or let the profile's ``own_object`` sources learn it from
    the player's first class resource change (a skill, a potion or regeneration).
    ``own_only`` yields only the player's positions. Queue loss and native
    drops fail closed like every capture session.
    """

    def __init__(self, *, profile: OpcodeProfile,
                 live_options: LiveCaptureOptions | None = None,
                 capture_seconds: float | None = None,
                 save_pcap: str | Path | None = None,
                 object_id: int | None = None,
                 own_only: bool = False) -> None:
        layout = profile_layout(profile)
        own = OwnObjectTracker(profile.own_object, object_id=object_id)
        super().__init__(label="Position", live_options=live_options, capture_seconds=capture_seconds,
                         save_pcap=save_pcap,
                         make_tracker=lambda publish: PositionTracker(layout, publish, own, own_only=own_only))
