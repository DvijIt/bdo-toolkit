"""Observed recent-draw encoding, bounds, and outcome-orientation regressions."""
from __future__ import annotations

import pytest

from bdo_toolkit.solare._details import (
    _decode_history_region, _history_outcome_counts, _history_matches_outcomes,
)
from bdo_toolkit.solare._detail_learning import _history_outcome_candidates, _orient_outcomes


@pytest.mark.parametrize("wire,counts", (
    ("-1,0", (0, 1, 0)),
    ("0,1,1,0,1,1,1,1,-1,0", (2, 1, 2)),
    ("-1,0,0,0,0,1", (1, 1, 1)),
    ("1,0,-1,0,1,1", (1, 1, 1)),
    (",".join(["-1,0"] * 5), (0, 5, 0)),
))
def test_draw_history_preserves_exact_signed_wire_values(wire, counts):
    decoded = _decode_history_region(wire.encode() + bytes(101-len(wire)))
    assert decoded == (wire, tuple(map(int, wire.split(','))))
    assert _history_outcome_counts(decoded[1]) == counts


@pytest.mark.parametrize("wire", (
    b"-1", b"-1,1", b"0,-1", b"1,-1", b"-1,-1", b"-2,0",
    b"2,0", b"-01,0", b"+1,0", b"-1,0,1", b"-1,0,", b"-1,,0",
    b" -1,0", b"-1, 0", b"\xff,0", b",".join([b"-1,0"]*6),
))
def test_unobserved_or_malformed_signed_history_is_rejected(wire):
    assert _decode_history_region(wire + b'\0') is None


def test_history_keeps_legacy_opaque_binary_and_empty_behavior():
    assert _decode_history_region(b'1,0,1\0') == ('1,0,1', (1,0,1))
    assert _history_outcome_counts((1,0,1)) is None
    assert _decode_history_region(b'\0stale inactive bytes') == ('', ())
    assert _decode_history_region(b'-1,0') is None


def _row(wins, draws, losses, *, older_wins=0):
    row = bytearray(160)
    pairs = [(1,1)]*wins + [(-1,0)]*draws + [(0,1)]*losses
    wire = ','.join(str(v) for pair in pairs for v in pair).encode()
    for offset,value in ((0,wins+draws+losses+older_wins),
                         (12,wins+older_wins),(24,draws),(36,losses)):
        row[offset:offset+4] = value.to_bytes(4,'little')
    row[48:48+len(wire)] = wire
    return bytes(row)


@pytest.mark.parametrize('matches', range(1,6))
def test_complete_short_history_with_draw_supports_exact_counters(matches):
    wins = max(0,matches-2)
    row = _row(wins,1,matches-wins-1)
    candidates = _history_outcome_candidates((row,), (1,), 0, (12,24,36), 48,
                                              minimum_discriminators=0)
    assert (12,24,36) in candidates
    assert _history_matches_outcomes((wins,1,matches-wins-1),matches,wins,1,matches-wins-1)


@pytest.mark.parametrize('older_wins', (0,10))
def test_draw_histories_independently_orient_complete_and_long_records(older_wins):
    rows = tuple(_row(w,d,l,older_wins=older_wins)
                 for w,d,l in ((3,1,1),(1,2,2),(1,3,1),(2,1,2),(0,1,4),(4,1,0)))
    assert _orient_outcomes(rows,(1,)*len(rows),0,(12,24,36),48) == (12,24,36)


@pytest.mark.parametrize('older_wins', (0,10))
def test_recent_draw_cannot_be_counted_as_lifetime_loss(older_wins):
    row = bytearray(_row(2,1,2,older_wins=older_wins))
    row[24:28] = bytes(4)
    row[36:40] = (3).to_bytes(4,'little')  # Arithmetic still balances.
    candidates = _history_outcome_candidates((bytes(row),),(1,),0,(12,24,36),48,
                                              minimum_discriminators=0)
    assert (12,24,36) not in candidates


def test_recent_counts_require_each_lifetime_component_and_complete_window():
    assert _history_matches_outcomes((2,1,2),81,43,1,37)
    for lifetime in ((1,1,79),(43,0,38),(79,1,1)):
        assert not _history_matches_outcomes((2,1,2),81,*lifetime)
    assert not _history_matches_outcomes((2,1,1),81,43,1,37)
    assert not _history_matches_outcomes((2,1,2),5,1,2,2)


def test_tied_binary_windows_do_not_weaken_existing_witness_minimum():
    rows = tuple(_row(w,d,l) for w,d,l in (
        (3,0,1),(1,0,3),(4,0,1),(1,0,4),(2,0,2),(2,0,2),(2,0,2)))
    assert _history_outcome_candidates(rows,(1,)*7,0,(12,24,36),48,
                                      minimum_discriminators=0) == ((12,24,36),)
    assert _orient_outcomes(rows,(1,)*7,0,(12,24,36),48) is None
