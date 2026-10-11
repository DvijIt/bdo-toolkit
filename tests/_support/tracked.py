"""Synthetic position, class resource and own-object messages on client 3464 geometry."""
import json
import struct

from bdo_toolkit._protocol import BDOFrame, FlowKey, PacketContext
from bdo_toolkit.profiles import (OwnObjectProfileLayout, OwnObjectSource, PositionProfileLayout, PositionSource,
                                  ResourceProfileLayout)

FLOW = FlowKey("192.0.2.1", 8889, "192.0.2.2", 40000)
ME = 0x00E864AC
OTHER_PLAYER = 0x01290CA0
MOUNT = 0x20086406

# 0x0C3F reports changes of movement, 0x1A21 steady updates (also walking backwards).
MOVE = PositionSource(0x0C3F, 46, 9, 21)
MOVE_FACING = PositionSource(0x0C3F, 46, 9, 21, facing_offset=29)
UPDATE = PositionSource(0x1A21, 73, 10, 37, facing_offset=25)
POSITION = PositionProfileLayout((MOVE,))
POSITION_FACING = PositionProfileLayout((MOVE_FACING, UPDATE))
RESOURCE = ResourceProfileLayout(0x1CB0, 46, 30, 34, 38, 42)
OWN_OBJECT = OwnObjectProfileLayout((OwnObjectSource(0x1CB0, 46, 38),))


def message(opcode: int, length: int, *fields: tuple[int, str, object], flag: int = 0) -> bytes:
    """A message of ``length`` bytes with ``(offset, struct format, value)`` fields over 0xED filler."""
    body = bytearray([0xED] * length)
    body[:2] = length.to_bytes(2, "little")
    body[2] = flag
    body[3:5] = opcode.to_bytes(2, "little")
    for offset, fmt, value in fields:
        values = value if isinstance(value, tuple) else (value,)
        struct.pack_into("<" + fmt, body, offset, *values)
    return bytes(body)


def frame(raw: bytes, *, at: float = 1.0, epoch: int = 1, flow: FlowKey = FLOW) -> BDOFrame:
    return BDOFrame(1, raw, PacketContext(at, flow, flow_generation=epoch), 1000)


def position(object_id: int = ME, x: float = 40429.0, y: float = -1602.25, z: float = -129983.875, *,
             source: PositionSource = MOVE, flag: int = 0,
             facing: tuple[float, float, float] = (0.0, 0.0, 1.0), **context: object) -> BDOFrame:
    """A position message; ``facing`` is written when the source has a facing offset."""
    fields: list[tuple[int, str, object]] = [(source.position_offset, "3f", (x, y, z)),
                                             (source.object_id_offset, "I", object_id)]
    if source.facing_offset is not None:
        fields.append((source.facing_offset, "3f", facing))
    raw = message(source.opcode, source.message_length, *fields, flag=flag)
    return frame(raw, **context)  # type: ignore[arg-type]


def resource(object_id: int = ME, current: int = 1754, change: int = -90, maximum: int = 1840, *,
             layout: ResourceProfileLayout = RESOURCE, **context: object) -> BDOFrame:
    raw = message(layout.opcode, layout.message_length, (layout.maximum_offset, "I", maximum),
                  (layout.change_offset, "i", change), (layout.object_id_offset, "I", object_id),
                  (layout.current_offset, "I", current))
    return frame(raw, **context)  # type: ignore[arg-type]


def profile_data(**layouts: object) -> dict[str, object]:
    """A minimal active profile holding the given layouts (None leaves one out)."""
    return {"version": 1, "profile_active": True, "specs": {},
            **{key: layout.to_dict() for key, layout in layouts.items() if layout is not None}}  # type: ignore[attr-defined]


def profile_file(tmp_path, **layouts: object):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile_data(**layouts)))
    return path


def write_capture(path, frames) -> None:
    from scapy.all import Ether, IP, TCP, Raw, wrpcap
    seq = 1000
    output = []
    for f in frames:
        flow = f.context.flow
        p = Ether() / IP(src=flow.source_ip, dst=flow.destination_ip) / TCP(
            sport=flow.source_port, dport=flow.destination_port, seq=seq, flags="PA") / Raw(f.message)
        p.time = f.context.timestamp
        output.append(p)
        seq += len(f.message)
    wrpcap(str(path), output)
