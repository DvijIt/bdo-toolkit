"""Synthetic spawn, hit and kill record lists on client 3464 geometry."""
import json
import struct

from bdo_toolkit._protocol import BDOFrame, FlowKey, PacketContext
from bdo_toolkit.profiles import CombatHitLayout, CombatKillLayout, CombatProfileLayout, CombatSpawnLayout

FLOW = FlowKey("192.0.2.1", 8889, "192.0.2.2", 40000)
ME = 0x00D6ECE6
OTHER_PLAYER = 0x00E2DCD5
BEE = 0x0D834400
BEETLE = 0x0D712C00
PARASITIC_BEE, GRASS_BEETLE = 20001, 20002

# Client 3464 geometry.
SPAWN = CombatSpawnLayout(0x19DE, header_length=14, count_offset=7, record_length=2789,
                          character_key_offset=52, object_id_offset=80)
HIT = CombatHitLayout(0x1025, header_length=10, count_offset=8, record_length=109, target_id_offset=30,
                      hit_id_offset=34, damage_offset=42, target_hp_offset=54, attacker_id_offset=58,
                      position_offset=62, skill_offset=95, critical_offset=103, special_attack_offset=102)
KILL = CombatKillLayout(0x0BEC, header_length=13, count_offset=8, record_length=189, position_offset=16,
                        character_key_offset=52, killer_id_offset=145, victim_id_offset=181)
COMBAT = CombatProfileLayout(SPAWN, HIT, KILL)
POSITION = (-160426.0, -437.25, 133507.0)

type Field = tuple[int, str, object]


def message(layout, records: list[list[Field]], *, count: int | None = None, flag: int = 0) -> bytes:
    """A record-list message over 0xED filler; ``fields`` offsets count from each record's start."""
    length = layout.header_length + layout.record_length * len(records)
    body = bytearray([0xED] * length)
    body[:2] = length.to_bytes(2, "little")
    body[2] = flag
    body[3:5] = layout.opcode.to_bytes(2, "little")
    body[layout.count_offset] = len(records) if count is None else count
    for index, fields in enumerate(records):
        start = layout.header_length + index * layout.record_length
        for offset, fmt, value in fields:
            values = value if isinstance(value, tuple) else (value,)
            struct.pack_into("<" + fmt, body, start + offset, *values)
    return bytes(body)


def frame(raw: bytes, *, at: float = 1.0, epoch: int = 1, flow: FlowKey = FLOW) -> BDOFrame:
    return BDOFrame(1, raw, PacketContext(at, flow, flow_generation=epoch), 1000)


def spawn_record(object_id: int, key: int) -> list[Field]:
    return [(SPAWN.character_key_offset, "H", key), (SPAWN.object_id_offset, "I", object_id)]


def hit_record(target: int = BEE, *, attacker: int = ME, hit_id: int = 0x024F1D35, damage: float = 2869.0,
               hp: float = 0.0, skill: int = 2621, critical: int = 0, special: int = 6) -> list[Field]:
    return [(HIT.target_id_offset, "I", target), (HIT.hit_id_offset, "I", hit_id), (HIT.damage_offset, "f", damage),
            (HIT.target_hp_offset, "f", hp), (HIT.attacker_id_offset, "I", attacker),
            (HIT.position_offset, "3f", POSITION), (HIT.skill_offset, "H", skill),
            (HIT.critical_offset, "B", critical), (HIT.special_attack_offset, "B", special)]


def kill_record(victim: int = BEE, *, killer: int = ME, key: int = PARASITIC_BEE) -> list[Field]:
    return [(KILL.position_offset, "3f", POSITION), (KILL.character_key_offset, "H", key),
            (KILL.killer_id_offset, "I", killer), (KILL.victim_id_offset, "I", victim)]


def spawns(*records: list[Field], **context) -> BDOFrame:
    return frame(message(SPAWN, list(records)), **context)


def hits(*records: list[Field], **context) -> BDOFrame:
    return frame(message(HIT, list(records)), **context)


def kills(*records: list[Field], **context) -> BDOFrame:
    return frame(message(KILL, list(records)), **context)


def profile_file(tmp_path, combat=COMBAT):
    path = tmp_path / "profile.json"
    data = {"version": 1, "profile_active": True, "specs": {}}
    if combat is not None:
        data["combat"] = combat.to_dict()
    path.write_text(json.dumps(data))
    return path


def write_capture(path, frames) -> None:
    """One server-to-client TCP stream per flow, in the given order."""
    from scapy.all import Ether, IP, TCP, Raw, wrpcap
    sequence: dict[FlowKey, int] = {}
    packets = []
    for f in frames:
        flow = f.context.flow
        seq = sequence.get(flow, 1000)
        p = Ether() / IP(src=flow.source_ip, dst=flow.destination_ip) / TCP(
            sport=flow.source_port, dport=flow.destination_port, seq=seq, flags="PA") / Raw(f.message)
        p.time = f.context.timestamp
        packets.append(p)
        sequence[flow] = seq + len(f.message)
    wrpcap(str(path), packets)
