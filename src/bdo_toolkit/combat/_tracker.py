"""Spawn, hit and kill decoder for live capture and replay."""
import math
import struct
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import NoReturn

from .._profile_runtime import validate_runtime_profile
from .._protocol import BDOFrame, FlowKey
from ..events import Flow
from ..profiles import CombatHitLayout, CombatKillLayout, CombatProfileLayout, CombatSpawnLayout, OpcodeProfile, ProfileError
from .models import CombatEvent, CombatHit, CombatKill, CombatSpawn, CombatStatus, CombatTrackingError

# Per connection; the oldest entries go first. A busy grind spot holds a few hundred objects.
MAX_KNOWN_OBJECTS = 65536
MAX_REMEMBERED_KILLS = 65536

_U16 = struct.Struct("<H")
_U32 = struct.Struct("<I")
_F32 = struct.Struct("<f")
_XYZ = struct.Struct("<3f")

type RecordLayout = CombatSpawnLayout | CombatHitLayout | CombatKillLayout


def profile_layout(profile: OpcodeProfile) -> CombatProfileLayout:
    if not isinstance(profile, OpcodeProfile):
        raise TypeError("profile must be a loaded OpcodeProfile")
    validate_runtime_profile(profile)
    if profile.combat is None:
        raise ProfileError("Profile has no combat layout; add one for this patch")
    return profile.combat


@dataclass
class _Connection:
    """What one connection epoch has told us: spawned objects and reported kills."""

    epoch: int
    characters: OrderedDict[int, int] = field(default_factory=OrderedDict)
    killed: OrderedDict[int, None] = field(default_factory=OrderedDict)


def _remember[K, V](entries: OrderedDict[K, V], key: K, value: V, limit: int) -> None:
    entries[key] = value
    entries.move_to_end(key)
    while len(entries) > limit:
        entries.popitem(last=False)


class CombatTracker:
    """Decodes every spawn, hit and kill record; a shape or value that does not fit withdraws authority."""

    def __init__(self, layout: CombatProfileLayout, on_event: Callable[[CombatEvent], None]) -> None:
        self.layout = layout
        self._publish = on_event
        self._connections: dict[FlowKey, _Connection] = {}
        self._events = 0
        self.reason: str | None = None

    def snapshot(self) -> CombatStatus:
        known = sum(len(c.characters) for c in self._connections.values())
        return CombatStatus("invalid" if self.reason else "tracking" if self._events else "waiting", known,
                            self.reason)

    def failed_snapshot(self, reason: str) -> CombatStatus:
        return CombatStatus("invalid", 0, reason)

    def invalidate(self, reason: str) -> NoReturn:
        self.reason = self.reason or reason
        raise CombatTrackingError(self.reason)

    def observe(self, frame: BDOFrame) -> None:
        if self.reason:
            raise CombatTrackingError(self.reason)
        opcode = frame.opcode
        if opcode == self.layout.spawn.opcode:
            self._observe_spawns(frame)
        elif opcode == self.layout.hit.opcode:
            self._observe_hits(frame)
        elif opcode == self.layout.kill.opcode:
            self._observe_kills(frame)

    def close_flow(self, flow: FlowKey) -> None:
        self._connections.pop(flow, None)

    def _connection(self, frame: BDOFrame) -> _Connection:
        flow, epoch = frame.context.flow, frame.context.flow_generation
        connection = self._connections.get(flow)
        if connection is None or connection.epoch != epoch:
            connection = self._connections[flow] = _Connection(epoch)
        return connection

    def _records(self, frame: BDOFrame, layout: RecordLayout, kind: str) -> range:
        """Start offsets of the message's records, after checking its shape."""
        message = frame.message
        body = len(message) - layout.header_length
        if (frame.flag != layout.flag or frame.length != len(message) or body <= 0
                or body % layout.record_length or message[layout.count_offset] != body // layout.record_length):
            self.invalidate(f"Combat {kind} message shape changed; update this patch's combat layout")
        if not math.isfinite(frame.context.timestamp):
            self.invalidate("Combat message has no valid timestamp")
        return range(layout.header_length, len(message), layout.record_length)

    def _emit(self, event: CombatEvent) -> None:
        self._events += 1
        self._publish(event)

    def _position(self, message: bytes, offset: int, kind: str) -> tuple[float, float, float]:
        position = _XYZ.unpack_from(message, offset)
        if not all(math.isfinite(value) for value in position):
            self.invalidate(f"Combat {kind} position failed validation; review the combat layout")
        return position

    def _observe_spawns(self, frame: BDOFrame) -> None:
        layout, message = self.layout.spawn, frame.message
        connection = self._connection(frame)
        for start in self._records(frame, layout, "spawn"):
            (key,) = _U16.unpack_from(message, start + layout.character_key_offset)
            (object_id,) = _U32.unpack_from(message, start + layout.object_id_offset)
            if connection.characters.get(object_id) == key:
                continue  # Sent again (e.g. a batch on zone load); nothing new.
            _remember(connection.characters, object_id, key, MAX_KNOWN_OBJECTS)
            self._emit(CombatSpawn(object_id, key, frame.context.timestamp, _flow(frame), connection.epoch))

    def _observe_hits(self, frame: BDOFrame) -> None:
        layout, message = self.layout.hit, frame.message
        connection = self._connection(frame)
        for start in self._records(frame, layout, "hit"):
            (hit_id,) = _U32.unpack_from(message, start + layout.hit_id_offset)
            if hit_id == 0:
                continue  # A record with no damage (unidentified; e.g. crowd control results).
            (damage,) = _F32.unpack_from(message, start + layout.damage_offset)
            (target_hp,) = _F32.unpack_from(message, start + layout.target_hp_offset)
            if not math.isfinite(damage) or not math.isfinite(target_hp) or damage < 0 or target_hp < 0:
                self.invalidate("Combat hit fields failed validation; review the combat layout")
            # 0 or 1 on every player's hit; a few monster attacks carry other values there.
            is_critical = message[start + layout.critical_offset] == 1
            (target_id,) = _U32.unpack_from(message, start + layout.target_id_offset)
            (attacker_id,) = _U32.unpack_from(message, start + layout.attacker_id_offset)
            (skill_no,) = _U16.unpack_from(message, start + layout.skill_offset)
            x, y, z = self._position(message, start + layout.position_offset, "hit")
            self._emit(CombatHit(target_id, attacker_id, skill_no, damage, target_hp, is_critical,
                                 message[start + layout.special_attack_offset], hit_id, x, y, z,
                                 connection.characters.get(target_id), frame.context.timestamp, _flow(frame),
                                 connection.epoch))

    def _observe_kills(self, frame: BDOFrame) -> None:
        layout, message = self.layout.kill, frame.message
        connection = self._connection(frame)
        for start in self._records(frame, layout, "kill"):
            (victim_id,) = _U32.unpack_from(message, start + layout.victim_id_offset)
            if victim_id in connection.killed:
                continue  # The server repeats a kill later, e.g. when the corpse comes back into view.
            (killer_id,) = _U32.unpack_from(message, start + layout.killer_id_offset)
            (key,) = _U16.unpack_from(message, start + layout.character_key_offset)
            x, y, z = self._position(message, start + layout.position_offset, "kill")
            _remember(connection.killed, victim_id, None, MAX_REMEMBERED_KILLS)
            self._emit(CombatKill(victim_id, killer_id, key, x, y, z, frame.context.timestamp, _flow(frame),
                                  connection.epoch))


def _flow(frame: BDOFrame) -> Flow:
    flow = frame.context.flow
    return Flow(flow.source_ip, flow.source_port, flow.destination_ip, flow.destination_port)
