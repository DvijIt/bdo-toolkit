"""Streaming offline combat replay; no implicit profile selection."""
from collections.abc import Iterable
from pathlib import Path

from .._protocol import DEFAULT_SERVER_PORTS
from .._tracked_capture import TrackedReplay
from ..profiles import OpcodeProfile
from ._tracker import CombatTracker, profile_layout
from .models import CombatEvent, CombatStatus

CombatReplay = TrackedReplay[CombatEvent, CombatStatus]


def replay_combat(path: str | Path, *, profile: OpcodeProfile,
                  ports: Iterable[int] = DEFAULT_SERVER_PORTS) -> CombatReplay:
    """Replay only with the recording's explicit matching combat profile."""
    layout = profile_layout(profile)
    return TrackedReplay(path, ports=ports, label="Combat",
                         make_tracker=lambda publish: CombatTracker(layout, publish))
