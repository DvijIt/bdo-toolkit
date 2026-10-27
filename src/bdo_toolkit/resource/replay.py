"""Streaming offline class resource replay; no implicit profile selection."""
from collections.abc import Iterable
from pathlib import Path

from .._own_object import OwnObjectTracker
from .._protocol import DEFAULT_SERVER_PORTS
from .._tracked_capture import TrackedReplay
from ..profiles import OpcodeProfile
from ._tracker import ResourceTracker, profile_layout
from .models import ResourceReading, ResourceStatus

ResourceReplay = TrackedReplay[ResourceReading, ResourceStatus]


def replay_resource(path: str | Path, *, profile: OpcodeProfile, ports: Iterable[int] = DEFAULT_SERVER_PORTS,
              object_id: int | None = None, own_only: bool = False) -> ResourceReplay:
    """Replay only with the recording's explicit matching resource profile.

    ``object_id`` names the player instead of learning it from the profile's
    ``own_object`` sources; ``own_only`` yields only the player's resource.
    """
    layout = profile_layout(profile)
    own = OwnObjectTracker(profile.own_object, object_id=object_id)
    return TrackedReplay(path, ports=ports, label="Resource",
                         make_tracker=lambda publish: ResourceTracker(layout, publish, own, own_only=own_only))
