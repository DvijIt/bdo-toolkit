"""Experimental class resource telemetry from an explicit profile layout.

The resource is MP or the class's own kind (one message for all classes).
Reports every change the server sends (the player's and nearby mounts'
stat) and marks the player's own once its object ID is known. The
opcode moves with most weekly patches.
"""
from ..profiles import OwnObjectProfileLayout, OwnObjectSource, ResourceProfileLayout
from .models import ResourceCaptureHealth, ResourceReading, ResourceStatus, ResourceTrackingError
from .replay import ResourceReplay, replay_resource
from .session import LiveResourceSession

__all__ = ["LiveResourceSession", "OwnObjectProfileLayout", "OwnObjectSource", "ResourceCaptureHealth",
           "ResourceProfileLayout", "ResourceReading", "ResourceReplay", "ResourceStatus", "ResourceTrackingError",
           "replay_resource"]
