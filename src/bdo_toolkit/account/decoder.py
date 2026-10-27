"""Profile-free decoders for account messages (EU, Oct 2026). Offsets are payload offsets.

Opcodes are fixed per game build and can be renumbered by a patch; pass a different
``AccountOpcodes`` when they change. Bundles (``AccountOpcodes.bundle``) carry several inner
messages, possibly split across consecutive messages, and are unwrapped before decoding.
"""
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from ..raw import RawMessage
from .models import (AccountRecord, CharacterInfo, CharacterSummary, ChatMessage, Experience, GearItem,
                     PlayerSighting, Servant, Worker)

ENCRYPTED_FLAG = 1

# Bundle header: u16 inner_len · u8 kind · u16 · u16 · u16 count · u32 body_len.
BUNDLE_HEADER = 13
CONTINUATION_HEADER = 7
KIND_COMPLETE, KIND_FIRST, KIND_CONTINUATION = 4, 1, 3

PLAYER_RECORD, PLAYER_LIST_HEADER = 270, 7
WORKER_RECORD, WORKER_LIST_HEADER, WORKER_SKILL_SLOTS = 62, 9, 9
GEAR_START, GEAR_SLOT, GEAR_SLOTS = 157, 221, 20
NAME_BYTES = 62


@dataclass(frozen=True)
class AccountOpcodes:
    """Server opcodes of the decoded messages; defaults observed on EU in Oct 2026."""

    bundle: int = 0x0D6F
    character_info: int = 0x16B8
    experience: int = 0x0CD2
    character_summary: int = 0x1CDB
    player_list: int = 0x10CB
    chat: int = 0x1978
    servant: int = 0x1B1D
    worker_list: int = 0x19DE


def _u16(b: bytes, at: int) -> int:
    return int.from_bytes(b[at:at + 2], "little")


def _u32(b: bytes, at: int) -> int:
    return int.from_bytes(b[at:at + 4], "little")


def _u64(b: bytes, at: int) -> int:
    return int.from_bytes(b[at:at + 8], "little")


def _utf16(b: bytes, at: int, max_bytes: int = NAME_BYTES) -> str:
    raw = b[at:at + max_bytes]
    for i in range(0, len(raw) - 1, 2):
        if raw[i] == 0 and raw[i + 1] == 0:
            raw = raw[:i]
            break
    return raw[:len(raw) // 2 * 2].decode("utf-16-le", errors="replace")


def _character_info(at: float, b: bytes) -> Iterator[AccountRecord]:
    if len(b) >= 7066 + 2:
        yield CharacterInfo(at, _u64(b, 13), _utf16(b, 7066), _utf16(b, 6597), _u32(b, 21),
                            _u32(b, 116), _u32(b, 120), _u32(b, 124))


def _experience(at: float, b: bytes) -> Iterator[AccountRecord]:
    if len(b) >= 42:
        yield Experience(at, level=_u32(b, 38), xp=_u64(b, 29), xp_to_next=_u64(b, 4))


def _character_summary(at: float, b: bytes) -> Iterator[AccountRecord]:
    if len(b) >= 84:
        yield CharacterSummary(at, _u64(b, 76), _utf16(b, 4))


def _player_list(at: float, b: bytes) -> Iterator[AccountRecord]:
    count = _u16(b, 0)
    if len(b) != PLAYER_LIST_HEADER + count * PLAYER_RECORD:
        return
    for i in range(count):
        r = b[PLAYER_LIST_HEADER + i * PLAYER_RECORD:PLAYER_LIST_HEADER + (i + 1) * PLAYER_RECORD]
        nick = _utf16(r, 112)
        if nick:
            yield PlayerSighting(at, _u64(r, 20), nick, _utf16(r, 188), r[176], r[252])


def _chat(at: float, b: bytes) -> Iterator[AccountRecord]:
    if len(b) > 316:
        yield ChatMessage(at, b[0], _utf16(b, 100), _utf16(b, 165), _utf16(b, 314, len(b) - 314))


def _servant(at: float, b: bytes) -> Iterator[AccountRecord]:
    if len(b) < GEAR_START:
        return
    name = _utf16(b, 34)
    if not name:
        return
    gear = []
    for slot in range(GEAR_SLOTS):
        a = GEAR_START + slot * GEAR_SLOT
        if a + 43 > len(b):
            break
        item_id = _u32(b, a)
        if item_id not in (0, 0xFFFFFFFF):
            gear.append(GearItem(slot, item_id, _u64(b, a + 4), _u16(b, a + 31), _u16(b, a + 33),
                                 _u64(b, a + 35)))
    yield Servant(at, _u32(b, 14), name, b[113], _u32(b, 105), tuple(gear))


def _worker_list(at: float, b: bytes) -> Iterator[AccountRecord]:
    count = _u16(b, 7)
    if len(b) < WORKER_LIST_HEADER + count * WORKER_RECORD:
        return
    for i in range(count):
        r = b[WORKER_LIST_HEADER + i * WORKER_RECORD:WORKER_LIST_HEADER + (i + 1) * WORKER_RECORD]
        skills = tuple(s for s in (_u16(r, 42 + 2 * k) for k in range(WORKER_SKILL_SLOTS)) if s)
        yield Worker(at, _u64(r, 0), _u16(r, 8), level=_u32(r, 18), stamina=_u16(r, 10),
                     town_id=_u32(r, 14), skills=skills)


def bundle_entries(body: bytes, count: int) -> Iterator[tuple[int, bytes]]:
    """Inner messages of a bundle body: u16 size (opcode + data) · u16 opcode · data."""
    off = 0
    for _ in range(count):
        if off + 4 > len(body):
            return
        size = _u16(body, off)
        yield _u16(body, off + 2), body[off + 4:off + 2 + size]
        off += 2 + size


class AccountDecoder:
    """Turns raw server messages into account records; feed messages in stream order.

    Stateful only for fragmented bundles, which span consecutive messages of one connection.
    Encrypted messages (flag 1) are skipped. Unknown opcodes yield nothing.
    """

    def __init__(self, opcodes: AccountOpcodes | None = None) -> None:
        self.opcodes = opcodes or AccountOpcodes()
        o = self.opcodes
        self._decoders: dict[int, Callable[[float, bytes], Iterator[AccountRecord]]] = {
            o.character_info: _character_info, o.experience: _experience,
            o.character_summary: _character_summary, o.player_list: _player_list, o.chat: _chat,
            o.servant: _servant, o.worker_list: _worker_list,
        }
        self._pending: bytearray | None = None
        self._pending_count = 0
        self._pending_len = 0
        self._pending_epoch: int | None = None

    def messages(self, opcode: int, payload: bytes, epoch: int | None = None) -> Iterator[tuple[int, bytes]]:
        """The message itself, or the inner messages of a bundle once it is complete."""
        if opcode != self.opcodes.bundle or len(payload) < CONTINUATION_HEADER:
            yield opcode, payload
            return
        kind = payload[2]
        if kind == KIND_COMPLETE and len(payload) >= BUNDLE_HEADER:
            yield from bundle_entries(payload[BUNDLE_HEADER:], _u16(payload, 7))
        elif kind == KIND_FIRST and len(payload) >= BUNDLE_HEADER:
            self._pending = bytearray(payload[BUNDLE_HEADER:])
            self._pending_count, self._pending_len = _u16(payload, 7), _u32(payload, 9)
            self._pending_epoch = epoch
        elif kind == KIND_CONTINUATION and self._pending is not None and epoch == self._pending_epoch:
            self._pending += payload[CONTINUATION_HEADER:]
            if len(self._pending) >= self._pending_len:
                body, self._pending = bytes(self._pending), None
                yield from bundle_entries(body, self._pending_count)

    def decode(self, opcode: int, payload: bytes, observed_at: float, epoch: int | None = None,
               ) -> list[AccountRecord]:
        records: list[AccountRecord] = []
        for inner_opcode, data in self.messages(opcode, payload, epoch):
            decode = self._decoders.get(inner_opcode)
            if decode is not None:
                records.extend(decode(observed_at, data))
        return records

    def feed(self, message: RawMessage) -> list[AccountRecord]:
        if message.flag == ENCRYPTED_FLAG:
            return []
        return self.decode(message.opcode, message.payload, message.observed_at, message.connection_epoch)
