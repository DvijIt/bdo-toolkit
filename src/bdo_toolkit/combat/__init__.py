"""Experimental spawn, hit and kill telemetry from an explicit profile layout.

Spawns name each object coming into view by its game data character key,
hits carry damage, the target's HP, critical, back and air attack, and
kills the killer. The opcodes move with most weekly patches, and record
shapes and offsets can move too; ``relocate_combat_replay`` /
``relocate_combat_live`` find all of them again from a short fight and
``update_combat_profile`` saves the result.
"""
from ..profiles import CombatHitLayout, CombatKillLayout, CombatProfileLayout, CombatSpawnLayout
from ._relocation import CombatCalibrationError, CombatRelocationEvidence
from .calibration import (CombatCalibrationResult, CombatProfileUpdate, relocate_combat_live, relocate_combat_replay,
                          update_combat_profile)
from .models import (AIR_ATTACK, BACK_ATTACK, PLAIN_HIT, CombatCaptureHealth, CombatEvent, CombatHit, CombatKill,
                     CombatSpawn, CombatStatus, CombatTrackingError)
from .replay import CombatReplay, replay_combat
from .session import LiveCombatSession

__all__ = ["AIR_ATTACK", "BACK_ATTACK", "PLAIN_HIT", "CombatCalibrationError", "CombatCalibrationResult",
           "CombatCaptureHealth", "CombatEvent", "CombatHit", "CombatHitLayout", "CombatKill", "CombatKillLayout",
           "CombatProfileLayout", "CombatProfileUpdate", "CombatRelocationEvidence", "CombatReplay", "CombatSpawn",
           "CombatSpawnLayout", "CombatStatus", "CombatTrackingError", "LiveCombatSession", "relocate_combat_live",
           "relocate_combat_replay", "replay_combat", "update_combat_profile"]
