"""Profile-free account decoders over synthetic payloads laid out like the EU Oct 2026 messages."""
import json

from bdo_toolkit.account import (AccountDecoder, AccountOpcodes, CharacterInfo, CharacterSummary, ChatMessage,
                                 Experience, GearItem, PlayerSighting, Servant, Worker)
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
    b = bytearray(58)
    put(b, 4, to_next, 8)
    put(b, 29, xp, 8)
    put(b, 38, level, 4)
    return bytes(b)


def character_info_payload():
    b = bytearray(7840)
    put(b, 13, 0x0000000000A11CE1, 8)
    put(b, 21, 34, 4)
    for at, fame in ((116, 2136), (120, 1669), (124, 901)):
        put(b, at, fame, 4)
    put_text(b, 6597, "Testfam")
    put_text(b, 7066, "Sefir")
    return bytes(b)


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
    b = bytearray(130)
    put(b, 0, 0x00010001, 4)
    put_text(b, 4, "Sefir")
    put(b, 76, 7, 8)
    put(b, 100, 61, 4)
    assert AccountDecoder().feed(raw(OPS.character_summary, bytes(b))) == [CharacterSummary(10.0, 7, "Sefir", 61)]


def test_player_list_requires_exact_record_count():
    record = bytearray(270)
    put(record, 20, 99, 8)
    put_text(record, 112, "PlayerOne")
    record[176] = 64
    put_text(record, 188, "FamilyOne")
    record[252] = 13
    payload = (1).to_bytes(2, "little") + b"\x00" * 5 + bytes(record)
    decoder = AccountDecoder()
    assert decoder.feed(raw(OPS.player_list, payload)) == [PlayerSighting(10.0, 99, "PlayerOne", "FamilyOne", 64, 13)]
    assert decoder.feed(raw(OPS.player_list, payload + b"\x00")) == []


def test_chat_message():
    b = bytearray(400)
    b[0] = 19
    put_text(b, 100, "FamilyTwo")
    put_text(b, 165, "PlayerTwo")
    put_text(b, 314, "see you later")
    (message,) = AccountDecoder().feed(raw(OPS.chat, bytes(b)))
    assert message == ChatMessage(10.0, 19, "FamilyTwo", "PlayerTwo", "see you later")
    assert message.channel_name == "world"


def test_servant_with_gear():
    b = bytearray(11481)
    put(b, 14, 31048, 4)
    put_text(b, 34, "Seabird")
    put(b, 105, 1, 4)
    b[113] = 1
    for slot in range(20):
        put(b, 157 + slot * 221, 0 if slot != 3 else 49706, 4)
    a = 157 + 3 * 221
    put(b, a + 4, 1, 8)
    put(b, a + 31, 100, 2)
    put(b, a + 33, 100, 2)
    put(b, a + 35, 0x0000000000B0B0B1, 8)
    (servant,) = AccountDecoder().feed(raw(OPS.servant, bytes(b)))
    assert servant == Servant(10.0, 31048, "Seabird", 1, 1, (GearItem(3, 49706, 1, 100, 100, 0x0000000000B0B0B1),))
    assert servant.category_name == "ship"


def test_worker_list():
    record = bytearray(62)
    put(record, 0, 0x0000000000C0FFEE, 8)
    put(record, 8, 7562, 2)
    put(record, 10, 10, 2)
    put(record, 14, 602, 4)
    put(record, 18, 27, 4)
    for k, skill in enumerate((1909, 1501, 1201)):
        put(record, 42 + 2 * k, skill, 2)
    payload = b"\x01\x00\x00\x00\x00\x00\x00" + (1).to_bytes(2, "little") + bytes(record)
    (worker,) = AccountDecoder().feed(raw(OPS.worker_list, payload))
    assert worker == Worker(10.0, 0x0000000000C0FFEE, 7562, level=27, stamina=10, town_id=602,
                            skills=(1909, 1501, 1201))
    assert (worker.type_name, worker.town_name) == ("Professional Goblin Worker", "Keplan")
    assert worker.skill_names == ("Ore Packing", "Lucky Guy A", "Thrifty A")
    assert Worker(1.0, 1, 1, 1, 1, 9999).town_name == "town#9999"


def test_custom_opcodes_after_a_patch():
    decoder = AccountDecoder(AccountOpcodes(experience=0x0AAA))
    assert decoder.feed(raw(OPS.experience, experience_payload())) == []
    assert decoder.feed(raw(0x0AAA, experience_payload()))[0].level == 61


def test_records_serialize_to_json():
    record = Worker(1.0, 2, 7552, 1, 8, 608, (1010,))
    assert json.loads(json.dumps(record.to_dict())) == {
        "schema_version": 1, "record_type": "worker", "observed_at": 1.0, "worker_id": 2, "type_id": 7552,
        "level": 1, "stamina": 8, "town_id": 608, "skills": [1010]}
