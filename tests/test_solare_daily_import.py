"""Daily archive safety and live orchestration tests; no live packet access."""
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from bdo_toolkit.solare import (
    SolareCaptureResult, SolareDetectionStatus, SolareEvidence,
    SolareLeaderboardSnapshot, SolarePlayer, SolareOverallEntry,
    SolareClass, SolareClassPerformance, SolareEquipment, SolareCaptureHealth,
)

spec = importlib.util.spec_from_file_location("daily", Path(__file__).resolve().parents[1] / "examples/solare_daily_import.py")
daily = importlib.util.module_from_spec(spec)
spec.loader.exec_module(daily)


@pytest.fixture
def result():
    equipment = tuple(SolareEquipment(slot, 741317, (15819, 15819)) for slot in daily.SLOTS)
    players = tuple(
        SolarePlayer(f"Player{i}", i + 1, SolareClass(i // 20, None), elo=1500,
                     classes_played=(SolareClassPerformance(0, True, SolareClass(i // 20, None),
                         matches=10, wins=6, draws=0, losses=4, equipment=equipment),))
        for i in range(640)
    )
    overall = tuple(SolareOverallEntry(p.name, p.global_rank, elo=p.elo, classes_played=p.classes_played) for p in players[:100])
    s = SolareLeaderboardSnapshot(snapshot_id="sha256:example", observed_at=1792145591.2424898,
        players=players, overall_top_100=overall,
        class_table_capabilities=frozenset(daily.REQUIRED | daily.DESIRED), overall_capabilities=frozenset(daily.REQUIRED | daily.DESIRED))
    return SolareCaptureResult(status=SolareDetectionStatus.COMPLETE, evidence=SolareEvidence(), snapshot=s)


def publish(tmp_path, result, **kwargs):
    source = tmp_path / "source.pcapng"
    source.write_bytes(b"synthetic capture provenance")
    return daily.import_daily(result, source, tmp_path / "site", region=kwargs.pop("region", "NA"),
                              season="2026-10", day_timezone="America/Los_Angeles", **kwargs)


def test_dates_regions_idempotence_and_history(tmp_path, result):
    index, version, selected = publish(tmp_path, result, day="2026-10-16")
    assert selected
    data = json.loads(version.read_text())
    assert data["region"] == "na" and data["leaderboard_date"] == "2026-10-16"
    original = version.read_bytes()
    assert publish(tmp_path, result, day="2026-10-16")[1] == version
    assert version.read_bytes() == original
    later = replace(result, snapshot=replace(result.snapshot, observed_at=result.snapshot.observed_at + 100, snapshot_id="new"))
    _, newer, selected = publish(tmp_path, later, day="2026-10-16")
    assert selected and newer != version
    assert not publish(tmp_path, result, day="2026-10-16")[2]
    content = json.loads(index.read_text())
    assert len(content["days"]["2026-10-16"]["versions"]) == 2
    assert content["days"]["2026-10-16"]["file"].endswith(newer.name)
    publish(tmp_path, result, day="2026-10-15")
    assert json.loads(index.read_text())["latest_date"] == "2026-10-16"
    eu_index, _, _ = publish(tmp_path, result, region="EU", day="2026-10-16")
    assert eu_index != index


def test_observation_date_and_explicit_override(tmp_path, result):
    _, version, _ = publish(tmp_path, result)
    value = json.loads(version.read_text())
    assert value["leaderboard_date"] == value["observed_date"]
    assert not value["date_overridden"]
    _, version, _ = publish(tmp_path, result, day="2026-10-15")
    assert json.loads(version.read_text())["date_overridden"]


@pytest.mark.parametrize("failure", ["incomplete", "dirty", "rankings", "count", "duplicate", "crystals"])
def test_rejection_does_not_change_archive(tmp_path, result, failure):
    index, _, _ = publish(tmp_path, result)
    original = index.read_bytes()
    if failure == "incomplete":
        bad = SolareCaptureResult(status=SolareDetectionStatus.NO_TRAFFIC, evidence=SolareEvidence())
    elif failure == "dirty":
        bad = replace(result, evidence=SolareEvidence(health=SolareCaptureHealth(tcp_gap_resets=1)))
    else:
        s = result.snapshot
        if failure == "rankings":
            s = replace(s, overall_capabilities=frozenset())
        elif failure == "count":
            s = replace(s, players=s.players[:-1])
        elif failure == "duplicate":
            s = replace(s, overall_top_100=(s.overall_top_100[0],) + s.overall_top_100[:-1])
        else:
            p = s.players[0]
            perf = replace(p.classes_played[0], equipment=(SolareEquipment(0, 1, (-1, 0)),))
            s = replace(s, players=(replace(p, classes_played=(perf,)),) + s.players[1:])
        bad = replace(result, snapshot=s)
    with pytest.raises(ValueError):
        publish(tmp_path, bad)
    assert index.read_bytes() == original


def test_lock_and_failed_index_publication_preserve_previous_day(tmp_path, result):
    index, _, _ = publish(tmp_path, result)
    original = index.read_bytes()
    lock = index.parent / ".import.lock"
    lock.touch()
    with pytest.raises(ValueError, match="locked"):
        publish(tmp_path, result)
    lock.unlink()
    with patch.object(daily.os, "replace", side_effect=OSError("disk error")):
        with pytest.raises(OSError):
            publish(tmp_path, result)
    assert index.read_bytes() == original and not lock.exists()


@pytest.mark.parametrize("region", ["../na", "NA/EU", "CON", "", "C:\\temp"])
def test_rejects_unsafe_region(tmp_path, result, region):
    with pytest.raises(ValueError):
        publish(tmp_path, result, region=region)


def test_live_prompts_records_replays_and_only_then_imports(tmp_path, result):
    def capture(**kwargs):
        assert kwargs["stop_on_complete"] is True
        assert kwargs["save_pcap"].parent == tmp_path / ".agents/solare-daily/captures"
        kwargs["save_pcap"].parent.mkdir(parents=True)
        kwargs["save_pcap"].write_bytes(b"capture")
        return result
    with patch.object(daily, "ROOT", tmp_path), patch("builtins.input", return_value="NA"), \
         patch.object(daily, "capture_solare_snapshot", side_effect=capture) as live, \
         patch.object(daily, "replay_solare", return_value=result) as replay:
        assert daily.main([]) == 0
    assert live.call_count == replay.call_count == 1
    assert (tmp_path / ".agents/solare-daily/site-data/na/2026-10/index.json").exists()


def test_live_replay_mismatch_never_imports(tmp_path, result):
    other = replace(result, snapshot=replace(result.snapshot, snapshot_id="different"))
    with patch.object(daily, "ROOT", tmp_path), \
         patch.object(daily, "capture_solare_snapshot", return_value=result), \
         patch.object(daily, "replay_solare", return_value=other):
        assert daily.main(["--region", "NA"]) == 1
    assert not (tmp_path / ".agents/solare-daily/site-data").exists()


def test_corrupt_existing_index_never_replaced(tmp_path, result):
    index, _, _ = publish(tmp_path, result)
    content = json.loads(index.read_text())
    selected = next(iter(content["days"].values()))
    selected["versions"] = ["../outside.json"]
    selected["file"] = "../outside.json"
    index.write_text(json.dumps(content))
    before = index.read_bytes()
    with pytest.raises(ValueError, match="path"):
        publish(tmp_path, result)
    assert index.read_bytes() == before


def test_timezone_cannot_silently_change(tmp_path, result):
    index, _, _ = publish(tmp_path, result)
    before = index.read_bytes()
    with pytest.raises(ValueError, match="metadata"):
        daily.import_daily(result, tmp_path / "source.pcapng", tmp_path / "site",
                           region="NA", season="2026-10", day_timezone="UTC")
    assert index.read_bytes() == before


def test_invalid_cli_never_starts_capture():
    with patch.object(daily, "capture_solare_snapshot") as capture:
        assert daily.main(["--date", "2026-10-16", "--region", "NA"]) == 1
        assert daily.main(["--capture-seconds", "nan", "--region", "NA"]) == 1
        assert daily.main(["--region", "../NA"]) == 1
    capture.assert_not_called()


def rankings_only(result):
    s = result.snapshot
    return replace(result, snapshot=replace(s, snapshot_id="rankings-only",
        players=tuple(replace(p, elo=None, classes_played=()) for p in s.players),
        overall_top_100=tuple(replace(p, elo=None, classes_played=()) for p in s.overall_top_100),
        class_table_capabilities=frozenset({"rankings"}), overall_capabilities=frozenset({"rankings"})))


def test_rankings_day_can_be_enriched_without_losing_original(tmp_path, result):
    partial = rankings_only(result)
    index, first, selected = publish(tmp_path, partial)
    assert selected
    payload = json.loads(first.read_text())
    assert payload["backfill_pending"]
    assert payload["missing_details"]["overall"] == ["elo", "equipment", "performance"]
    assert "elo" not in payload["snapshot"]["players"][0]
    _, complete, selected = publish(tmp_path, result)
    assert selected and first.exists() and complete != first
    assert not json.loads(complete.read_text())["backfill_pending"]
    assert not publish(tmp_path, partial)[2]
    # Even a new poorer decode at the same timestamp cannot downgrade selection.
    worse = replace(partial, snapshot=replace(partial.snapshot, snapshot_id="another-poor-decode"))
    assert not publish(tmp_path, worse)[2]
    day = next(iter(json.loads(index.read_text())["days"].values()))
    assert len(day["versions"]) == 3 and day["file"].endswith(complete.name)


def test_new_day_with_only_rankings_is_published(tmp_path, result):
    index, old, _ = publish(tmp_path, result)
    partial = rankings_only(result)
    partial = replace(partial, snapshot=replace(partial.snapshot, observed_at=partial.snapshot.observed_at + 86400))
    _, new, selected = publish(tmp_path, partial)
    assert selected and old.exists() and new.exists()
    assert json.loads(index.read_text())["latest_date"] == "2026-10-17"
