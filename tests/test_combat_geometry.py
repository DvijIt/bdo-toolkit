"""Record list geometry found again from message lengths and the count byte."""
from bdo_toolkit.combat._geometry import Geometry, fallback_geometries, solved_geometries

HIT = Geometry(header_length=10, count_offset=8, record_length=109)


def record_list(geometry: Geometry, count: int, filler: int = 0xED) -> bytes:
    body = bytearray([filler] * (geometry.header_length + geometry.record_length * count))
    body[geometry.count_offset] = count
    return bytes(body)


def test_two_record_counts_fix_header_count_offset_and_record_length():
    moved = Geometry(header_length=12, count_offset=9, record_length=113)
    messages = [record_list(moved, n) for n in (1, 3, 1, 2)]
    assert solved_geometries(messages) == (moved,)


def test_one_record_count_fixes_nothing():
    assert solved_geometries([record_list(HIT, 1)] * 3) == ()


def test_a_message_that_does_not_fit_rules_the_geometry_out():
    messages = [record_list(HIT, 1), record_list(HIT, 2), record_list(HIT, 2) + b"\x00"]
    assert solved_geometries(messages) == ()


def test_single_count_messages_keep_the_previous_geometry_when_it_fits():
    assert fallback_geometries([record_list(HIT, 1)] * 2, HIT) == (HIT,)


def test_single_count_messages_keep_the_previous_header_with_a_new_record_length():
    longer = Geometry(HIT.header_length, HIT.count_offset, HIT.record_length + 4)
    assert fallback_geometries([record_list(longer, 1)] * 2, HIT) == (longer,)


def test_single_count_messages_of_different_lengths_offer_nothing():
    longer = Geometry(HIT.header_length, HIT.count_offset, HIT.record_length + 4)
    assert fallback_geometries([record_list(longer, 1), record_list(longer, 1) + b"\x00"], HIT) == ()
