"""Experimental spawn, hit and kill telemetry from an explicit profile layout.

Spawns name each object coming into view by its game data character key,
hits carry damage, the target's HP, critical, back and air attack, and
kills the killer. The opcodes move with most weekly patches.
"""
from ..profiles import CombatHitLayout, CombatKillLayout, CombatProfileLayout, CombatSpawnLayout
from .models import (AIR_ATTACK, BACK_ATTACK, PLAIN_HIT, CombatCaptureHealth, CombatEvent, CombatHit, CombatKill,
                     CombatSpawn, CombatStatus, CombatTrackingError)
from .replay import CombatReplay, replay_combat
from .session import LiveCombatSession

__all__ = ["AIR_ATTACK", "BACK_ATTACK", "PLAIN_HIT", "CombatCaptureHealth", "CombatEvent", "CombatHit",
           "CombatHitLayout", "CombatKill", "CombatKillLayout", "CombatProfileLayout", "CombatReplay", "CombatSpawn",
           "CombatSpawnLayout", "CombatStatus", "CombatTrackingError", "LiveCombatSession", "replay_combat"]
