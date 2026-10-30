"""Profile-free decoders for account messages (EU, build of the 29 Oct 2026 patch). Offsets are payload offsets.

Opcodes are fixed per game build and can be renumbered by a patch; pass a different
``AccountOpcodes`` when they change. The 29 Oct 2026 patch also moved fields inside most
messages, so new opcodes alone do not decode an older build. Bundles (``AccountOpcodes.bundle``)
carry several inner messages, possibly split across consecutive messages, and are unwrapped
before decoding.
"""
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace

from ..raw import RawMessage
from .models import (AccountRecord, CharacterInfo, CharacterSummary, ChatMessage, CompletedQuests, Contribution,
                     Equipment, Experience, GearItem, KnowledgeBatch, KnowledgeEntry, LifeEquipment, NodeBatch,
                     NodeState, OpenQuests, PlayerSighting, QuestKey, Servant, SkillPoints, Worker)

ENCRYPTED_FLAG = 1

# Bundle header: u16 inner_len · u8 kind · u16 · u16 · u16 count · u32 body_len.
BUNDLE_HEADER = 13
CONTINUATION_HEADER = 7
KIND_COMPLETE, KIND_FIRST, KIND_CONTINUATION = 4, 1, 3

PLAYER_RECORD, PLAYER_LIST_HEADER = 269, 2
WORKER_RECORD, WORKER_LIST_HEADER, WORKER_SKILL_SLOTS = 62, 4, 9
GEAR_START, GEAR_SLOT, GEAR_SLOTS = 161, 221, 20
# Gear slots: u32 item id · u64 count · … · u16 durability @31 · u16 max @33 · u64 instance id @35.
GEAR_ITEM = 43
EQUIPMENT_SLOTS = 46
LIFE_EQUIPMENT_HEADER, LIFE_EQUIPMENT_RECORD = 5, GEAR_SLOT + 2
KNOWLEDGE_RECORD, KNOWLEDGE_HEADER = 14, 6
NODE_RECORD, NODE_HEADER, NODE_INVESTED = 15, 8, 4
CONTRIBUTION_SIZE, SKILL_POINTS_SIZE = 15, 10
# (record size, header size, offset of the u16 count, offset of the key in a record)
COMPLETED_QUESTS = (15, 4, 0, 3)
OPEN_QUESTS = (47, 8, 1, 8)
NAME_BYTES = 62


@dataclass(frozen=True)
class AccountOpcodes:
    """Server opcodes of the decoded messages; defaults observed on EU after the 29 Oct 2026 patch.

    Before that patch they were: bundle 0x0D6F, character_info 0x16B8, experience 0x0CD2,
    character_summary 0x1CDB, player_list 0x10CB, chat 0x1978, servant 0x1B1D, worker_list 0x19DE,
    knowledge 0x0DB6, knowledge_gain 0x0E42, contribution 0x118D, skill_points 0x0D0E,
    node_list 0x0FA2, completed_quests 0x0CDA, open_quests 0x0C5A, equipment 0x12BF and
    life_equipment 0x1405. ``knowledge_gain`` has not been seen since the patch and keeps its old
    opcode and layout until a capture of gaining a knowledge confirms them.
    """

    bundle: int = 0x0D36
    character_info: int = 0x19B0
    experience: int = 0x0C04
    character_summary: int = 0x0E17
    player_list: int = 0x15EC
    chat: int = 0x1ADE
    servant: int = 0x107E
    worker_list: int = 0x1372
    knowledge: int = 0x16D6
    knowledge_gain: int = 0x0E42
    contribution: int = 0x1112
    skill_points: int = 0x1729
    node_list: int = 0x1BEF
    completed_quests: int = 0x123A
    open_quests: int = 0x1B39
    equipment: int = 0x0EA0
    life_equipment: int = 0x0E9E


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
    # The id, class, slot and fame block sits after the family (+6458) and nick (+6927).
    if len(b) >= 7821 + 4:
        yield CharacterInfo(at, _u64(b, 7710), _utf16(b, 6927), _utf16(b, 6458), _u16(b, 7718),
                            _u32(b, 7813), _u32(b, 7817), _u32(b, 7821), slot=_u16(b, 7720))


def _experience(at: float, b: bytes) -> Iterator[AccountRecord]:
    if len(b) >= 45:
        yield Experience(at, level=_u32(b, 0), xp=_u64(b, 8), xp_to_next=_u64(b, 37))


def _character_summary(at: float, b: bytes) -> Iterator[AccountRecord]:
    if len(b) >= 82:
        yield CharacterSummary(at, _u64(b, 74), _utf16(b, 2))


def _player_list(at: float, b: bytes) -> Iterator[AccountRecord]:
    count = _u16(b, 0)
    if len(b) != PLAYER_LIST_HEADER + count * PLAYER_RECORD:
        return
    for i in range(count):
        r = b[PLAYER_LIST_HEADER + i * PLAYER_RECORD:PLAYER_LIST_HEADER + (i + 1) * PLAYER_RECORD]
        nick = _utf16(r, 113)
        if nick:
            yield PlayerSighting(at, _u64(r, 21), nick, _utf16(r, 189), r[177], r[253])


def _chat(at: float, b: bytes) -> Iterator[AccountRecord]:
    if len(b) > 314:
        yield ChatMessage(at, b[301], _utf16(b, 1), _utf16(b, 158), _utf16(b, 312, len(b) - 312))


def _gear_item(slot: int, b: bytes, a: int) -> GearItem | None:
    """The item of a gear slot at ``a``, or None for an empty slot."""
    item_id = _u32(b, a)
    if item_id in (0, 0xFFFFFFFF):
        return None
    return GearItem(slot, item_id, _u64(b, a + 4), _u16(b, a + 31), _u16(b, a + 33), _u64(b, a + 35))


def _servant(at: float, b: bytes) -> Iterator[AccountRecord]:
    if len(b) < GEAR_START:
        return
    name = _utf16(b, 45)
    if not name:
        return
    gear = []
    for slot in range(GEAR_SLOTS):
        a = GEAR_START + slot * GEAR_SLOT
        if a + GEAR_ITEM > len(b):
            break
        item = _gear_item(slot, b, a)
        if item is not None:
            gear.append(item)
    yield Servant(at, _u32(b, 22), name, b[144], _u32(b, 121), tuple(gear))


def _equipment(at: float, b: bytes) -> Iterator[AccountRecord]:
    # 8-byte header · one gear slot per equipment slot, in slot order. Slots are read from the end,
    # which also fits the 14-byte header used before the 29 Oct 2026 patch.
    start = len(b) - EQUIPMENT_SLOTS * GEAR_SLOT
    if start < 0:
        return
    items = (_gear_item(slot, b, start + slot * GEAR_SLOT) for slot in range(EQUIPMENT_SLOTS))
    yield Equipment(at, None, tuple(item for item in items if item is not None))


def _life_equipment(at: float, b: bytes) -> Iterator[AccountRecord]:
    # u16 count · 3 bytes · count × (gear slot · u16 slot); empty slots are not sent.
    count = _u16(b, 0)
    if len(b) != LIFE_EQUIPMENT_HEADER + count * LIFE_EQUIPMENT_RECORD:
        return
    items = (_gear_item(_u16(b, a + GEAR_SLOT), b, a)
             for a in range(LIFE_EQUIPMENT_HEADER, len(b), LIFE_EQUIPMENT_RECORD))
    yield LifeEquipment(at, tuple(item for item in items if item is not None))


def _worker_list(at: float, b: bytes) -> Iterator[AccountRecord]:
    count = _u16(b, 1)
    if len(b) < WORKER_LIST_HEADER + count * WORKER_RECORD:
        return
    for i in range(count):
        r = b[WORKER_LIST_HEADER + i * WORKER_RECORD:WORKER_LIST_HEADER + (i + 1) * WORKER_RECORD]
        skills = tuple(s for s in (_u16(r, 42 + 2 * k) for k in range(WORKER_SKILL_SLOTS)) if s)
        yield Worker(at, _u64(r, 0), _u16(r, 8), level=_u32(r, 18), stamina=_u16(r, 10),
                     town_id=_u32(r, 14), skills=skills)


def _knowledge(at: float, b: bytes) -> Iterator[AccountRecord]:
    # u32 ecology points · u16 count · count × (u8 · u32 id · 3 bytes · u8 level · 5 bytes unknown).
    count = _u16(b, 4)
    if len(b) != KNOWLEDGE_HEADER + count * KNOWLEDGE_RECORD:
        return
    entries = tuple(
        KnowledgeEntry(_u32(b, a + 1), b[a + 8])
        for a in range(KNOWLEDGE_HEADER, len(b), KNOWLEDGE_RECORD)
    )
    yield KnowledgeBatch(at, _u32(b, 0), entries)


def _knowledge_gain(at: float, b: bytes) -> Iterator[AccountRecord]:
    # Layout before the 29 Oct 2026 patch: u16 · u32 knowledge_id · u32 ecology_points · u8 level · 3 bytes.
    if len(b) >= 11:
        yield KnowledgeBatch(at, _u32(b, 6), (KnowledgeEntry(_u32(b, 2), b[10]),))


def _contribution(at: float, b: bytes) -> Iterator[AccountRecord]:
    # u64 experience · u16 total · u16 available · 3 bytes; 194/410 → 195/411 seen restart experience.
    if len(b) == CONTRIBUTION_SIZE:
        yield Contribution(at, available=_u16(b, 10), total=_u16(b, 8), experience=_u64(b, 0))


def _skill_points(at: float, b: bytes) -> Iterator[AccountRecord]:
    if len(b) == SKILL_POINTS_SIZE:
        yield SkillPoints(at, available=_u16(b, 8), total=_u16(b, 6))


def _node_list(at: float, b: bytes) -> Iterator[AccountRecord]:
    # u8 · u16 count · 5 bytes · count × (u32 node · u8 invested · …).
    count = _u16(b, 1)
    if len(b) != NODE_HEADER + count * NODE_RECORD:
        return
    nodes = tuple(
        NodeState(_u32(b, a), b[a + NODE_INVESTED] == 1)
        for a in range(NODE_HEADER, len(b), NODE_RECORD)
    )
    yield NodeBatch(at, nodes)


def _quest_keys(b: bytes, layout: tuple[int, int, int, int]) -> tuple[QuestKey, ...] | None:
    record, header, count_at, key_at = layout
    count = _u16(b, count_at)
    if len(b) != header + count * record:
        return None
    return tuple(QuestKey(_u16(b, a + key_at), _u16(b, a + key_at + 2)) for a in range(header, len(b), record))


def _completed_quests(at: float, b: bytes) -> Iterator[AccountRecord]:
    quests = _quest_keys(b, COMPLETED_QUESTS)
    if quests is not None:
        yield CompletedQuests(at, quests)


def _open_quests(at: float, b: bytes) -> Iterator[AccountRecord]:
    quests = _quest_keys(b, OPEN_QUESTS)
    if quests is not None:
        yield OpenQuests(at, quests)


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

    Stateful for fragmented bundles, which span consecutive messages of one connection, and for
    the character of ``Equipment``, taken from the latest ``CharacterInfo``.
    Encrypted messages (flag 1) are skipped. Unknown opcodes yield nothing.
    """

    def __init__(self, opcodes: AccountOpcodes | None = None) -> None:
        self.opcodes = opcodes or AccountOpcodes()
        o = self.opcodes
        self._decoders: dict[int, Callable[[float, bytes], Iterator[AccountRecord]]] = {
            o.character_info: _character_info, o.experience: _experience,
            o.character_summary: _character_summary, o.player_list: _player_list, o.chat: _chat,
            o.servant: _servant, o.worker_list: _worker_list, o.knowledge: _knowledge, o.knowledge_gain: _knowledge_gain,
            o.contribution: _contribution, o.skill_points: _skill_points, o.node_list: _node_list,
            o.completed_quests: _completed_quests, o.open_quests: _open_quests,
            o.equipment: _equipment, o.life_equipment: _life_equipment,
        }
        self._character_id: int | None = None
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
            if decode is None:
                continue
            for record in decode(observed_at, data):
                if isinstance(record, CharacterInfo):
                    self._character_id = record.character_id
                elif isinstance(record, Equipment):
                    record = replace(record, character_id=self._character_id)
                records.append(record)
        return records

    def feed(self, message: RawMessage) -> list[AccountRecord]:
        if message.flag == ENCRYPTED_FLAG:
            return []
        return self.decode(message.opcode, message.payload, message.observed_at, message.connection_epoch)
