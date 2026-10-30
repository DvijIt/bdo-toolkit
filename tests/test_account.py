"""Profile-free account decoders over synthetic payloads laid out like the EU Oct 2026 messages."""
import json

from bdo_toolkit.account import (AccountDecoder, AccountOpcodes, CharacterInfo, CharacterSummary, ChatMessage,
                                 CompletedQuests, Contribution, Equipment, Experience, GearItem, KnowledgeBatch,
                                 KnowledgeEntry, LifeEquipment, NodeBatch, NodeState, OpenQuests, PlayerSighting,
                                 QuestKey, Servant, SkillPoints, Worker)
from bdo_toolkit.events import Flow
from bdo_toolkit.raw import RawMessage

OPS = AccountOpcodes()
FLOW = Flow("192.0.2.1", 8889, "192.0.2.2", 40000)


def put(buf, at, value, size):
    buf[at:at + size] = value.to_bytes(size, "little")


def put_text(buf, at, text):
    data = text.encode("utf-16-le")
    buf[at:at + len(data)] = data


def raw(opcode, payload, *, flag=0, at=10.0, epoch=1):
    message = (5 + len(payload)).to_bytes(2, "little") + bytes([flag]) + opcode.to_bytes(2, "little") + payload
    return RawMessage(message, at, FLOW, epoch, 0, None)


def bundle_body(*inner):
    return b"".join((len(data) + 2).to_bytes(2, "little") + op.to_bytes(2, "little") + data for op, data in inner)


def bundle(body, count, kind=4):
    header = (len(body) + 6).to_bytes(2, "little") + bytes([kind]) + b"\x58\x14\x00\x00"
    return header + count.to_bytes(2, "little") + len(body).to_bytes(4, "little") + body


def experience_payload(level=61, xp=125_316_039, to_next=234_278_708):
    b = bytearray(61)
    put(b, 0, level, 4)
    put(b, 8, xp, 8)
    put(b, 37, to_next, 8)
    return bytes(b)


def character_info_payload(class_id=34, slot=0):
    b = bytearray(7839)
    put(b, 7710, 0x0000000000A11CE1, 8)
    put(b, 7718, class_id, 2)
    put(b, 7720, slot, 2)
    for at, fame in ((7813, 2136), (7817, 1669), (7821, 901)):
        put(b, at, fame, 4)
    put_text(b, 6458, "Testfam")
    put_text(b, 6927, "Sefir")
    return bytes(b)


def test_default_opcodes_follow_the_october_29_patch():
    assert (OPS.bundle, OPS.character_info, OPS.experience, OPS.character_summary, OPS.player_list, OPS.chat,
            OPS.servant, OPS.worker_list, OPS.knowledge, OPS.contribution, OPS.skill_points, OPS.node_list,
            OPS.completed_quests, OPS.open_quests, OPS.equipment, OPS.life_equipment) == (
        0x0D36, 0x19B0, 0x0C04, 0x0E17, 0x15EC, 0x1ADE, 0x107E, 0x1372, 0x16D6, 0x1112, 0x1729, 0x1BEF,
        0x123A, 0x1B39, 0x0EA0, 0x0E9E)


def test_character_and_experience_are_unwrapped_from_a_bundle():
    body = bundle_body((0x0EC1, b"\x00" * 58), (OPS.character_info, character_info_payload()),
                       (OPS.experience, experience_payload()))
    records = AccountDecoder().feed(raw(OPS.bundle, bundle(body, 3), at=5.0))
    assert records == [
        CharacterInfo(5.0, 0x0000000000A11CE1, "Sefir", "Testfam", 34, 2136, 1669, 901),
        Experience(5.0, level=61, xp=125_316_039, xp_to_next=234_278_708),
    ]
    assert records[0].class_name == "Dosa"
    assert round(records[1].percent, 3) == 53.490


def test_class_and_slot_are_separate_fields():
    # Mirel: Deadeye in the second slot arrived as 0x00010023 when read as one u32.
    (info,) = AccountDecoder().feed(raw(OPS.character_info, character_info_payload(class_id=35, slot=1)))
    assert (info.class_id, info.slot, info.class_name) == (35, 1, "Deadeye")


def test_fragmented_bundle_completes_on_its_continuation():
    body = bundle_body((OPS.experience, experience_payload(level=12)), (0x0EBB, b"\xaa" * 40))
    first = bundle(body[:30], 2, kind=1)
    first = first[:9] + len(body).to_bytes(4, "little") + first[13:]
    continuation = (len(body) - 30 + 5).to_bytes(2, "little") + b"\x03\x58\x14\x00\x00" + body[30:]
    decoder = AccountDecoder()
    assert decoder.feed(raw(OPS.bundle, first)) == []
    assert decoder.feed(raw(OPS.bundle, continuation)) == [Experience(10.0, 12, 125_316_039, 234_278_708)]


def test_continuation_from_another_connection_is_ignored():
    body = bundle_body((OPS.experience, experience_payload()))
    first = bundle(body[:20], 1, kind=1)
    first = first[:9] + len(body).to_bytes(4, "little") + first[13:]
    continuation = b"\x00\x00\x03\x58\x14\x00\x00" + body[20:]
    decoder = AccountDecoder()
    decoder.feed(raw(OPS.bundle, first, epoch=1))
    assert decoder.feed(raw(OPS.bundle, continuation, epoch=2)) == []


def test_encrypted_and_unknown_messages_yield_nothing():
    decoder = AccountDecoder()
    assert decoder.feed(raw(OPS.experience, experience_payload(), flag=1)) == []
    assert decoder.feed(raw(0x1111, b"\x01\x02")) == []


def test_character_summary():
    b = bytearray(138)
    put(b, 0, 1, 2)
    put_text(b, 2, "Sefir")
    put(b, 74, 7, 8)
    put(b, 98, 61, 4)
    # +98 held 61 after the character reached 62, so it is not reported as the level.
    assert AccountDecoder().feed(raw(OPS.character_summary, bytes(b))) == [CharacterSummary(10.0, 7, "Sefir")]


def test_player_list_requires_exact_record_count():
    record = bytearray(269)
    put(record, 21, 99, 8)
    put_text(record, 113, "PlayerOne")
    record[177] = 64
    put_text(record, 189, "FamilyOne")
    record[253] = 13
    payload = (1).to_bytes(2, "little") + bytes(record)
    decoder = AccountDecoder()
    assert decoder.feed(raw(OPS.player_list, payload)) == [PlayerSighting(10.0, 99, "PlayerOne", "FamilyOne", 64, 13)]
    assert decoder.feed(raw(OPS.player_list, payload + b"\x00")) == []


def test_chat_message():
    b = bytearray(400)
    b[301] = 19
    put_text(b, 1, "FamilyTwo")
    put_text(b, 158, "PlayerTwo")
    put_text(b, 312, "see you later")
    (message,) = AccountDecoder().feed(raw(OPS.chat, bytes(b)))
    assert message == ChatMessage(10.0, 19, "FamilyTwo", "PlayerTwo", "see you later")
    assert message.channel_name == "world"


def test_servant_with_gear():
    b = bytearray(11485)
    put(b, 22, 31048, 4)
    put_text(b, 45, "Seabird")
    put(b, 121, 1, 4)
    b[144] = 1
    for slot in range(20):
        put(b, 161 + slot * 221, 0 if slot != 3 else 49706, 4)
    a = 161 + 3 * 221
    put(b, a + 4, 1, 8)
    put(b, a + 31, 100, 2)
    put(b, a + 33, 100, 2)
    put(b, a + 35, 0x0000000000B0B0B1, 8)
    (servant,) = AccountDecoder().feed(raw(OPS.servant, bytes(b)))
    assert servant == Servant(10.0, 31048, "Seabird", 1, 1, (GearItem(3, 49706, 1, 100, 100, 0x0000000000B0B0B1),))
    assert servant.category_name == "ship"


def gear_slot(item_id, durability=(100, 100), instance=0x0000000000B0B0B1):
    b = bytearray(221)
    put(b, 0, item_id, 4)
    put(b, 4, 1, 8)
    put(b, 31, durability[0], 2)
    put(b, 33, durability[1], 2)
    put(b, 35, instance, 8)
    return bytes(b)


def equipment_payload(items, header=8):
    # Header · 46 gear slots in slot order; an empty slot holds item id 0.
    return bytes(header) + b"".join(gear_slot(items[slot]) if slot in items else bytes(221) for slot in range(46))


def test_equipment_belongs_to_the_character_that_entered_the_world():
    # Sorentik: Sovereign Hwando VIII (747222 with 8 in the top byte), awakening at slot 29.
    items = {0: 8 << 24 | 747222, 7: 7 << 24 | 11697, 29: 20 << 24 | 745727}
    body = bundle_body((OPS.character_info, character_info_payload()), (OPS.equipment, equipment_payload(items)))
    records = AccountDecoder().feed(raw(OPS.bundle, bundle(body, 2), at=5.0))
    assert records[1] == Equipment(5.0, 0x0000000000A11CE1, tuple(
        GearItem(slot, item_id, 1, 100, 100, 0x0000000000B0B0B1) for slot, item_id in items.items()))
    assert records[1].slot_names == ("main weapon", "necklace", "awakening weapon")
    assert [(g.base_item_id, g.enhancement_level) for g in records[1].gear] == [(747222, 8), (11697, 7), (745727, 20)]


def test_equipment_without_character_info_and_with_the_header_before_october_29():
    decoder = AccountDecoder()
    (equipment,) = decoder.feed(raw(OPS.equipment, equipment_payload({40: 10 << 24 | 16162}, header=14)))
    assert equipment == Equipment(10.0, None, (GearItem(40, 10 << 24 | 16162, 1, 100, 100, 0x0000000000B0B0B1),))
    assert equipment.slot_names == ("slot#40",)
    assert decoder.feed(raw(OPS.equipment, bytes(46 * 221 - 1))) == []


def test_life_equipment():
    # u16 count · 3 bytes · count × (gear slot · u16 slot), as in the family's life gear message.
    records = ((0, 18 << 24 | 720508), (52, 45302))
    payload = len(records).to_bytes(2, "little") + b"\x01\x00\x00" + b"".join(
        gear_slot(item_id) + slot.to_bytes(2, "little") for slot, item_id in records)
    decoder = AccountDecoder()
    (life,) = decoder.feed(raw(OPS.life_equipment, payload))
    assert life == LifeEquipment(10.0, tuple(GearItem(slot, item_id, 1, 100, 100, 0x0000000000B0B0B1)
                                             for slot, item_id in records))
    assert life.slot_names == ("lumbering axe", "alchemy stone")
    assert decoder.feed(raw(OPS.life_equipment, payload[:-1])) == []


def test_worker_list():
    record = bytearray(62)
    put(record, 0, 0x0000000000C0FFEE, 8)
    put(record, 8, 7562, 2)
    put(record, 10, 10, 2)
    put(record, 14, 602, 4)
    put(record, 18, 27, 4)
    for k, skill in enumerate((1909, 1501, 1201)):
        put(record, 42 + 2 * k, skill, 2)
    payload = b"\x01" + (1).to_bytes(2, "little") + b"\x00" + bytes(record)
    (worker,) = AccountDecoder().feed(raw(OPS.worker_list, payload))
    assert worker == Worker(10.0, 0x0000000000C0FFEE, 7562, level=27, stamina=10, town_id=602,
                            skills=(1909, 1501, 1201))
    assert (worker.type_name, worker.town_name) == ("Professional Goblin Worker", "Keplan")
    assert worker.skill_names == ("Ore Packing", "Lucky Guy A", "Thrifty A")
    assert Worker(1.0, 1, 1, 1, 1, 9999).town_name == "town#9999"


def knowledge_payload(points, entries):
    body = b"".join(b"\x00" + kid.to_bytes(4, "little") + b"\x00\x00\xff" + bytes([level]) + b"\x00\x00\x00\x00\x6c"
                    for kid, level in entries)
    return points.to_bytes(4, "little") + len(entries).to_bytes(2, "little") + body


def test_knowledge_batch():
    payload = knowledge_payload(2596, [(1, 1), (2, 5), (15054, 3)])
    (batch,) = AccountDecoder().feed(raw(OPS.knowledge, payload))
    assert batch == KnowledgeBatch(10.0, 2596, (KnowledgeEntry(1, 1), KnowledgeEntry(2, 5), KnowledgeEntry(15054, 3)))


def test_knowledge_batch_with_a_wrong_count_is_skipped():
    payload = knowledge_payload(2596, [(1, 1), (2, 5)])
    assert AccountDecoder().feed(raw(OPS.knowledge, payload + b"\x00")) == []


def test_custom_opcodes_after_a_patch():
    decoder = AccountDecoder(AccountOpcodes(experience=0x0AAA))
    assert decoder.feed(raw(OPS.experience, experience_payload())) == []
    assert decoder.feed(raw(0x0AAA, experience_payload()))[0].level == 61


def test_records_serialize_to_json():
    record = Worker(1.0, 2, 7552, 1, 8, 608, (1010,))
    assert json.loads(json.dumps(record.to_dict())) == {
        "schema_version": 1, "record_type": "worker", "observed_at": 1.0, "worker_id": 2, "type_id": 7552,
        "level": 1, "stamina": 8, "town_id": 608, "skills": [1010]}


def test_contribution_points():
    b = bytearray(15)
    put(b, 0, 3_699, 8)
    put(b, 8, 411, 2)
    put(b, 10, 195, 2)
    (record,) = AccountDecoder().feed(raw(OPS.contribution, bytes(b)))
    assert record == Contribution(10.0, available=195, total=411, experience=3_699)
    assert AccountDecoder().feed(raw(OPS.contribution, bytes(b) + b"\x00")) == []


def test_skill_points():
    b = bytearray(10)
    put(b, 0, 0x0220, 2)
    put(b, 2, 0x48C3, 2)
    put(b, 4, 117, 2)
    put(b, 6, 1892, 2)
    put(b, 8, 183, 2)
    (record,) = AccountDecoder().feed(raw(OPS.skill_points, bytes(b)))
    assert record == SkillPoints(10.0, available=183, total=1892)


def node_list_payload(nodes):
    b = bytearray(8 + 15 * len(nodes))
    b[0] = 1
    put(b, 1, len(nodes), 2)
    for i, (node_id, invested) in enumerate(nodes):
        a = 8 + 15 * i
        put(b, a, node_id, 4)
        b[a + 4] = int(invested)
        b[a + 13] = 1
    return bytes(b)


def test_node_list_marks_invested_nodes():
    payload = node_list_payload([(1, True), (1509, False), (301, True)])
    (record,) = AccountDecoder().feed(raw(OPS.node_list, payload))
    assert record == NodeBatch(10.0, (NodeState(1, True), NodeState(1509, False), NodeState(301, True)))
    assert record.invested == (1, 301)
    assert AccountDecoder().feed(raw(OPS.node_list, payload[:-1])) == []


def quest_payload(keys, record, header, count_at, key_at):
    b = bytearray(header + record * len(keys))
    put(b, count_at, len(keys), 2)
    for i, (group, index) in enumerate(keys):
        a = header + record * i
        b[a:a + key_at] = bytes(range(1, key_at + 1))  # змінні байти перед ключем
        put(b, a + key_at, group, 2)
        put(b, a + key_at + 2, index, 2)
    return bytes(b)


def test_completed_and_open_quests():
    done = quest_payload([(40022, 14), (748, 1)], 15, 4, 0, 3)
    (record,) = AccountDecoder().feed(raw(OPS.completed_quests, done))
    assert record == CompletedQuests(10.0, (QuestKey(40022, 14), QuestKey(748, 1)))
    open_ = quest_payload([(6814, 1)], 47, 8, 1, 8)
    (record,) = AccountDecoder().feed(raw(OPS.open_quests, open_))
    assert record == OpenQuests(10.0, (QuestKey(6814, 1),))
    assert AccountDecoder().feed(raw(OPS.completed_quests, done[:-1])) == []


def test_knowledge_gain_on_the_fly():
    # Opcode 0x0E42: u16 · u32 knowledge_id · u32 ecology_points · u8 level · 3 bytes.
    payload = bytes.fromhex("737cf6040000240a000001000000")
    (record,) = AccountDecoder().feed(raw(OPS.knowledge_gain, payload, at=15.0))
    assert record == KnowledgeBatch(15.0, 2596, (KnowledgeEntry(1270, 1),))
