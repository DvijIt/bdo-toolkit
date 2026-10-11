"""Immutable raw server-to-client messages; nothing is decoded or decrypted."""
from dataclasses import dataclass

from .._tracked_capture import CaptureHealth
from ..events import Flow


@dataclass(frozen=True)
class RawMessage:
    """One length-framed server-to-client message, exactly as received.

    ``message`` is the whole frame: u16 little-endian length (header
    included), u8 flag, u16 little-endian opcode, then the payload. Flags and
    payloads are reported verbatim; the toolkit assigns them no meaning.
    ``index`` counts messages within one connection epoch. ``stream_sequence``
    is the unwrapped TCP sequence of the first byte, so a gap between two
    consecutive messages of one epoch stays visible.
    """

    message: bytes
    observed_at: float
    flow: Flow
    connection_epoch: int
    index: int
    stream_sequence: int | None

    @property
    def length(self) -> int:
        return int.from_bytes(self.message[0:2], "little")

    @property
    def flag(self) -> int:
        return self.message[2]

    @property
    def opcode(self) -> int:
        return int.from_bytes(self.message[3:5], "little")

    @property
    def payload(self) -> bytes:
        return self.message[5:]

    def to_dict(self) -> dict[str, object]:
        return {"schema_version": 1, "event_type": "raw_message", "observed_at": self.observed_at,
                "flow": self.flow.to_dict(), "connection_epoch": self.connection_epoch,
                "index": self.index, "stream_sequence": self.stream_sequence, "length": self.length,
                "flag": self.flag, "opcode": f"0x{self.opcode:04X}", "payload": self.payload.hex()}


RawCaptureHealth = CaptureHealth
