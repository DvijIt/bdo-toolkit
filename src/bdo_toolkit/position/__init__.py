"""Experimental world position telemetry from an explicit profile layout.

Reports every nearby moving object and marks the player's own once its
object ID is known: given, or learned from the profile's ``own_object``
sources (the player's class resource change). Positions never reveal the ID themselves.
Patches move the opcodes and can change the message lengths and field
order; ``relocate_position_replay`` / ``relocate_position_live`` find all
three again and ``update_position_profile`` saves the result.
"""
from .._layout_relocation import LayoutProfileUpdate, LayoutRelocationError
from ..profiles import OwnObjectProfileLayout, OwnObjectSource, PositionProfileLayout, PositionSource
from .models import PositionCaptureHealth, PositionReading, PositionStatus, PositionTrackingError
from .calibration import (PositionCalibrationResult, relocate_position_live, relocate_position_replay,
                          update_position_profile)
from .replay import PositionReplay, replay_position
from .session import LivePositionSession

__all__ = ["LayoutProfileUpdate", "LayoutRelocationError", "LivePositionSession", "OwnObjectProfileLayout",
           "OwnObjectSource", "PositionCalibrationResult", "PositionCaptureHealth", "PositionProfileLayout",
           "PositionReading", "PositionReplay", "PositionSource", "PositionStatus", "PositionTrackingError",
           "relocate_position_live", "relocate_position_replay", "replay_position", "update_position_profile"]
