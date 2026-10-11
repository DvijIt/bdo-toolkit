"""Immutable spawn, hit and kill observations of objects near the player."""
from dataclasses import asdict, dataclass
from typing import Literal

from .._tracked_capture import CaptureHealth
from ..events import Flow

# Special attack values on client 3464: 6 on a plain hit, 0 on a back attack,
# 4 on an air attack. 2 also carries a damage bonus but is not identified yet
# (probably down attack).
PLAIN_HIT = 6
BACK_ATTACK = 0
AIR_ATTACK = 4


class CombatTrackingError(RuntimeError):
    """The combat layout no longer fits the traffic; stop using events."""


def _hex_ids(values: dict[str, object], *names: str) -> dict[str, object]:
    return {**values, **{name: f"0x{values[name]:08X}" for name in names}}


@dataclass(frozen=True)
class CombatSpawn:
    """An object came into view: its object ID and character key.

    ``character_key`` is the game data's character ID (``characterstatic``),
    which names the monster, NPC, player class or destructible object.
    ``object_id`` is new for every spawn and names nothing by itself; hits
    and kills refer to it. A spawn the server sends again is not repeated.
    """

    object_id: int
    character_key: int
    observed_at: float
    flow: Flow
    connection_epoch: int

    def to_dict(self) -> dict[str, object]:
        return {**_hex_ids(asdict(self), "object_id"), "schema_version": 1, "event_type": "combat_spawn"}


@dataclass(frozen=True)
class CombatHit:
    """One hit on one target, by the player or anyone else nearby.

    ``damage`` and ``target_hp`` (after the hit) are the server's values;
    the game shows no damage numbers. x, y (height), z is the target's
    position in game centimetres. ``target_character_key`` comes from the
    target's spawn on this connection and is None when the target came into
    view before the capture started. ``hit_id`` names the attack step: every
    hit of that step, and every later use of it, repeats it.
    ``special_attack`` is the raw value (see ``is_back_attack`` and
    ``is_air_attack``).
    """

    target_id: int
    attacker_id: int
    skill_no: int
    damage: float
    target_hp: float
    is_critical: bool
    special_attack: int
    hit_id: int
    x: float
    y: float
    z: float
    target_character_key: int | None
    observed_at: float
    flow: Flow
    connection_epoch: int

    @property
    def is_back_attack(self) -> bool:
        return self.special_attack == BACK_ATTACK

    @property
    def is_air_attack(self) -> bool:
        return self.special_attack == AIR_ATTACK

    @property
    def is_lethal(self) -> bool:
        """The target has no HP left after this hit."""
        return self.target_hp <= 0

    def to_dict(self) -> dict[str, object]:
        return {**_hex_ids(asdict(self), "target_id", "attacker_id", "hit_id"), "schema_version": 1,
                "event_type": "combat_hit", "is_back_attack": self.is_back_attack,
                "is_air_attack": self.is_air_attack, "is_lethal": self.is_lethal}


@dataclass(frozen=True)
class CombatKill:
    """An object was killed (or a destructible object destroyed).

    Reported once per object and connection: the server's later repeats
    (when the corpse comes back into view) are dropped. x, y, z is where it
    died. Loot arrives as separate item receipts and is not linked here.
    """

    victim_id: int
    killer_id: int
    character_key: int
    x: float
    y: float
    z: float
    observed_at: float
    flow: Flow
    connection_epoch: int

    def to_dict(self) -> dict[str, object]:
        return {**_hex_ids(asdict(self), "victim_id", "killer_id"), "schema_version": 1,
                "event_type": "combat_kill"}


CombatEvent = CombatSpawn | CombatHit | CombatKill


@dataclass(frozen=True)
class CombatStatus:
    """``waiting`` until the first event; ``known_objects`` counts spawns remembered."""

    status: Literal["waiting", "tracking", "invalid"]
    known_objects: int = 0
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


CombatCaptureHealth = CaptureHealth
