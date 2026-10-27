"""Public single-use combat capture with explicit profile authority."""
from pathlib import Path

from .._capture_options import LiveCaptureOptions
from .._tracked_capture import TrackedLiveSession
from ..profiles import OpcodeProfile
from ._tracker import CombatTracker, profile_layout
from .models import CombatEvent, CombatStatus


class LiveCombatSession(TrackedLiveSession[CombatEvent, CombatStatus]):
    """Spawns, hits and kills of objects near the player from an explicit profile layout.

    Every hit and kill the server reports nearby is yielded, the player's and
    other players'; compare ``attacker_id`` or ``killer_id`` with the
    player's object ID to keep only the player's own. Queue loss and native
    drops fail closed like every capture session.
    """

    def __init__(self, *, profile: OpcodeProfile,
                 live_options: LiveCaptureOptions | None = None,
                 capture_seconds: float | None = None,
                 save_pcap: str | Path | None = None) -> None:
        layout = profile_layout(profile)
        super().__init__(label="Combat", live_options=live_options, capture_seconds=capture_seconds,
                         save_pcap=save_pcap, make_tracker=lambda publish: CombatTracker(layout, publish))
