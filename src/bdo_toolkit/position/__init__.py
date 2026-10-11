"""Experimental world position telemetry from an explicit profile layout.

Reports every nearby moving object and marks the player's own once its
object ID is known: given, or learned from the profile's ``own_object``
sources (the player's class resource change). Positions never reveal the ID themselves.
The opcodes move with most weekly patches.
"""
from ..profiles import OwnObjectProfileLayout, OwnObjectSource, PositionProfileLayout, PositionSource
from .models import PositionCaptureHealth, PositionReading, PositionStatus, PositionTrackingError
from .replay import PositionReplay, replay_position
from .session import LivePositionSession

__all__ = ["LivePositionSession", "OwnObjectProfileLayout", "OwnObjectSource", "PositionCaptureHealth",
           "PositionProfileLayout", "PositionReading", "PositionReplay", "PositionSource", "PositionStatus",
           "PositionTrackingError", "replay_position"]
