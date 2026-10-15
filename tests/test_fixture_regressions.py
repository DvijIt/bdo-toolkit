"""Regression tests for approved current-contract fixture outputs.

Each private fixture is decoded with its recorded profile authority and
compared with its reviewed baseline. Any diff means the current TCP
reassembly, frame-scanning, or event-decoding contract changed.

The pcap fixtures are personal captures kept out of the public repository;
these tests skip automatically when they are not present locally.
"""

import json
from pathlib import Path

import pytest

from fixture_paths import (
    all_fixture_pcaps,
    baseline_path_for_fixture,
    optional_fixture_path,
    opcode_profile_for_fixture,
)
from bdo_toolkit import replay_pcap

SEMANTIC_EVENT_FIELDS = (
    "event_type",
    "timestamp",
    "flow",
    "item_id",
    "quantity",
    "source",
    "raw_context",
    "opcode",
    "message_length",
    "base_item_id",
    "enhancement_level",
    "enhancement",
    "inventory_slot",
    "item_instance",
    "storage_instance",
    "storage_id",
    "storage_name",
    "storage_name_confidence",
    "record_index",
    "record_count",
    "record_offset",
    "confidence",
)


def _has_reviewed_positive_baseline(pcap: Path) -> bool:
    baseline = baseline_path_for_fixture(pcap)
    return baseline.is_file() and bool(baseline.read_text(encoding="utf-8").strip())


def _semantic_event(payload: dict[str, object]) -> dict[str, object]:
    return {
        field: payload[field]
        for field in SEMANTIC_EVENT_FIELDS
        if field in payload
    }


# Empty historical baselines do not prove that silence is correct, especially
# when their capture has no matching profile sidecar. Only reviewed positive
# outputs participate in this broad matrix; focused tests own fail-closed and
# intentionally silent scenarios.
FIXTURES = [
    pcap for pcap in all_fixture_pcaps() if _has_reviewed_positive_baseline(pcap)
]

pytestmark = pytest.mark.skipif(
    not FIXTURES,
    reason="local pcap fixtures not present (private captures)",
)


@pytest.mark.parametrize("pcap", FIXTURES, ids=lambda path: path.stem)
def test_fixture_matches_baseline(pcap: Path):
    baseline_path = baseline_path_for_fixture(pcap)
    assert baseline_path.exists(), f"missing baseline for {pcap.name}"

    expected = [
        _semantic_event(json.loads(line))
        for line in baseline_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if pcap == optional_fixture_path('inventory--character-switch--4016c4bfa8'):
        # Preserve the reviewed historical baseline byte-for-byte. The 2026-10-09
        # registry correction changes only these two inferred display names:
        # all 2,530 events were compared; 54 names per ID differed and every
        # other semantic field matched. Actual output is never normalized.
        corrected_names = {
            229: ("Shakatu", "Valencia City"),
            601: ("Valencia City", "Shakatu"),
        }
        for payload in expected:
            correction = corrected_names.get(payload.get("storage_id"))
            if correction is not None:
                old_name, new_name = correction
                assert payload["storage_name"] == old_name
                assert payload["storage_name_confidence"] == "inferred"
                payload["storage_name"] = new_name
    # Every reviewed baseline uses its catalog-pinned profile; missing authority
    # is an error rather than a fallback to another generation.
    actual = [
        _semantic_event(event.to_dict())
        for event in replay_pcap(
            pcap,
            opcode_profile=opcode_profile_for_fixture(pcap),
        )
    ]

    assert actual == expected
