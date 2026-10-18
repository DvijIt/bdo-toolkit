"""Live capture or offline backfill into a versioned daily website archive.

Run from the repository root. Defaults keep all generated data local/ignored.
Only deploy the output directory, never the recordings or diagnostic reports.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bdo_toolkit import PacketCaptureOptions, __version__
from bdo_toolkit.solare import capture_solare_snapshot, replay_solare

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = {"rankings"}
DESIRED = {"elo", "performance", "equipment"}
SLOTS = (0, 1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 27, 29)
# An operator-supplied season label, not a season inferred from packets.
DEFAULT_SEASON = "2026-10"


def slug(value):
    value = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value):
        raise ValueError("Region/season must use 1-64 letters, numbers, hyphens or underscores")
    if value.split(".")[0] in {"con", "prn", "aux", "nul", *[f"com{i}" for i in range(1, 10)], *[f"lpt{i}" for i in range(1, 10)]}:
        raise ValueError("Reserved filesystem name")
    return value


def validate(result):
    """Layer website requirements over the parser's protocol/integrity gates."""
    if not result.complete or result.snapshot is None:
        raise ValueError(f"No complete snapshot: {result.message}")
    if not result.evidence.health.capture_is_clean:
        raise ValueError("Capture integrity failed; the archive was not updated")
    s = result.snapshot
    for label, caps in (("class", s.class_table_capabilities), ("overall", s.overall_capabilities)):
        missing = REQUIRED - caps
        if missing:
            raise ValueError(f"{label} table lacks required capabilities: {sorted(missing)}")
    if not math.isfinite(s.observed_at) or s.observed_at <= 0:
        raise ValueError("Invalid observation timestamp")
    counts = Counter(p.primary_class.code for p in s.players)
    if len(s.players) not in (620, 640) or set(counts.values()) != {20}:
        raise ValueError("Expected complete per-class top-20 boards")
    if len(s.overall_top_100) != 100 or {p.global_rank for p in s.overall_top_100} != set(range(1, 101)):
        raise ValueError("Expected overall ranks 1 through 100")
    for rows, caps in ((s.players, s.class_table_capabilities), (s.overall_top_100, s.overall_capabilities)):
        if len({p.name for p in rows}) != len(rows) or len({p.global_rank for p in rows}) != len(rows):
            raise ValueError("Duplicate player names or ranks within a table")
        for player in rows:
            if "elo" in caps and player.elo is None:
                raise ValueError("Elo capability claims missing values")
            if ("performance" in caps or "equipment" in caps) and not 1 <= len(player.classes_played) <= 3:
                raise ValueError("Detail capability claims missing class slots")
            for perf in player.classes_played:
                if ("performance" in caps or any(getattr(perf, k) is not None for k in ("matches", "wins", "draws", "losses"))) and perf.record_is_balanced is not True:
                    raise ValueError("Unbalanced or missing performance counters")
                if perf.equipment is None:
                    if "equipment" in caps:
                        raise ValueError("Equipment capability claims missing slots")
                    continue
                if tuple(e.slot_code for e in perf.equipment) != SLOTS:
                    raise ValueError("Invalid equipment slots")
                if any(e.item_id <= 0 or len(e.crystal_ids) != 2 or any(c < 0 for c in e.crystal_ids) for e in perf.equipment):
                    raise ValueError("Invalid equipment or crystal IDs")
    return s


def write_json_atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(".tmp-" + uuid4().hex[:16])
    try:
        with temp.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def import_daily(result, source, output, *, region, season, day_timezone, day=None):
    snapshot = validate(result)
    region, season = slug(region), slug(season)
    observed_day = datetime.fromtimestamp(snapshot.observed_at, ZoneInfo(day_timezone)).date().isoformat()
    chosen_day = date.fromisoformat(day).isoformat() if day else observed_day
    with Path(source).open("rb") as stream:
        capture_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    payload = {
        "archive_schema_version": 1,
        "region": region, "season_id": season,
        "leaderboard_date": chosen_day, "date_timezone": day_timezone,
        "observed_date": observed_day, "date_overridden": chosen_day != observed_day,
        "parser_version": __version__,
        "importer_version": 2,
        "missing_details": {
            "class_table": sorted(DESIRED - snapshot.class_table_capabilities),
            "overall": sorted(DESIRED - snapshot.overall_capabilities),
        },
        "backfill_pending": not (DESIRED <= snapshot.class_table_capabilities and DESIRED <= snapshot.overall_capabilities),
        "source_capture_sha256": capture_hash,
        "snapshot": snapshot.to_dict(),
    }
    # Content-addressed versions retain recaptures and parser improvements.
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    relative = f"snapshots/{chosen_day}-{digest}.json"
    folder = Path(output) / region / season
    folder.mkdir(parents=True, exist_ok=True)
    lock = folder / ".import.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise ValueError(f"Archive is locked: {lock}. If no importer is running, remove this stale lock and retry.") from None
    try:
        os.close(fd)
        index_path = folder / "index.json"
        if index_path.exists():
            index = json.loads(index_path.read_text(encoding="utf-8"))
            expected = (1, region, season, day_timezone)
            actual = tuple(index.get(k) for k in ("archive_schema_version", "region", "season_id", "date_timezone"))
            if actual != expected or not isinstance(index.get("days"), dict):
                raise ValueError("Existing archive metadata is incompatible; use its timezone or a different output directory")
        else:
            index = {"archive_schema_version": 1, "region": region, "season_id": season,
                     "date_timezone": day_timezone, "latest_date": None, "days": {}}
        for archive_day, archived in index["days"].items():
            date.fromisoformat(archive_day)
            if not isinstance(archived, dict):
                raise ValueError("Invalid existing daily index entry")
            versions = archived.get("versions")
            if not isinstance(versions, list) or not versions or archived.get("file") not in versions:
                raise ValueError("Invalid existing snapshot version list")
            timestamp = archived.get("observed_at")
            if not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
                raise ValueError("Invalid existing daily observation time")
            for filename in versions:
                if not isinstance(filename, str) or not re.fullmatch(r"snapshots/" + re.escape(archive_day) + r"-[0-9a-f]{64}\.json", filename):
                    raise ValueError("Invalid existing archive snapshot path")
                if not (folder / filename).is_file():
                    raise ValueError("Existing archive points to a missing snapshot")
        if index["latest_date"] != (max(index["days"]) if index["days"] else None):
            raise ValueError("Existing archive latest date is inconsistent")
        version_path = folder / relative
        if version_path.exists():
            existing = json.loads(version_path.read_text(encoding="utf-8"))
            if {k: v for k, v in existing.items() if k != "imported_at"} != payload:
                raise ValueError("Existing snapshot content does not match its identity")
        else:
            write_json_atomic(version_path, {**payload, "imported_at": datetime.now(timezone.utc).isoformat()})
        entry = index["days"].get(chosen_day)
        if entry is None:
            entry = {"file": relative, "observed_at": snapshot.observed_at,
                     "snapshot_id": snapshot.snapshot_id, "versions": []}
            index["days"][chosen_day] = entry
        if relative not in entry["versions"]:
            entry["versions"].append(relative)
            # An older backfill never replaces a later observation. A new
            # decode of the same observation may expose additional semantics.
            newer = snapshot.observed_at > entry["observed_at"]
            same_time = snapshot.observed_at == entry["observed_at"]
            selected_snapshot = json.loads((folder / entry["file"]).read_text(encoding="utf-8"))["snapshot"]
            preserves_details = (
                set(selected_snapshot["class_table_capabilities"]) <= snapshot.class_table_capabilities
                and set(selected_snapshot["overall_capabilities"]) <= snapshot.overall_capabilities
            )
            if newer or (same_time and preserves_details):
                entry.update(file=relative, observed_at=snapshot.observed_at,
                             snapshot_id=snapshot.snapshot_id,
                             missing_details=payload["missing_details"],
                             backfill_pending=payload["backfill_pending"])
        index["latest_date"] = max(index["days"])
        # Sole publication point: immutable snapshot is durable before index.
        write_json_atomic(index_path, index)
        return index_path, version_path, entry["file"] == relative
    finally:
        lock.unlink()


def progress(update):
    print(f"[{update.kind.value}] {update.message}", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", help="region label (prompted when omitted, e.g. NA, EU, SEA)")
    parser.add_argument("--season", default=DEFAULT_SEASON, help="operator season label (default: %(default)s)")
    parser.add_argument("--timezone", default="America/Los_Angeles", help="daily boundary timezone (default: %(default)s)")
    parser.add_argument("--output", type=Path, default=ROOT / ".agents/solare-daily/site-data")
    parser.add_argument("--replay", type=Path, help="backfill a saved PCAP instead of live capture")
    parser.add_argument("--date", help="explicit YYYY-MM-DD assignment, allowed only with --replay")
    parser.add_argument("--capture-seconds", type=float, default=120)
    parser.add_argument("--iface")
    parser.add_argument("--local-ip")
    args = parser.parse_args(argv)
    try:
        if args.date and not args.replay:
            raise ValueError("--date is for offline backfills only; live captures use their observation date")
        if args.date:
            date.fromisoformat(args.date)
        ZoneInfo(args.timezone)
        if not math.isfinite(args.capture_seconds) or args.capture_seconds <= 0:
            raise ValueError("--capture-seconds must be positive and finite")
        region = slug(args.region or input("Region (NA / EU / SEA / other region code): "))
        season = slug(args.season)
        print(f"Region={region.upper()}, season={season}, day timezone={args.timezone}", flush=True)
        run_id = uuid4().hex
        diagnostics = ROOT / ".agents/solare-daily/diagnostics" / run_id
        if args.replay:
            source = args.replay.resolve(strict=True)
        else:
            source = ROOT / ".agents/solare-daily/captures" / f"solare-{region}-{run_id}.pcapng"
            print(f"Recording: {source}\nWait for capture-ready, then manually open Solare > Leaderboard. Auto-stop is enabled.", flush=True)
            live = capture_solare_snapshot(
                capture_options=PacketCaptureOptions(interface=args.iface, local_ip=args.local_ip),
                capture_seconds=args.capture_seconds, save_pcap=source,
                stop_on_complete=True, on_update=progress,
            )
            write_json_atomic(diagnostics.with_suffix(".live.json"), live.to_dict())
            validate(live)
            print("[verify] Replaying the saved recording before publication...", flush=True)
        result = replay_solare(source)
        write_json_atomic(diagnostics.with_suffix(".replay.json"), result.to_dict())
        validate(result)
        if not args.replay and live.snapshot.snapshot_id != result.snapshot.snapshot_id:
            raise ValueError("Live/replay snapshot identities disagree; archive unchanged")
        index, version, selected = import_daily(
            result, source, args.output, region=region, season=season,
            day_timezone=args.timezone, day=args.date,
        )
        missing = {
            "class_table": sorted(DESIRED - result.snapshot.class_table_capabilities),
            "overall": sorted(DESIRED - result.snapshot.overall_capabilities),
        }
        if any(missing.values()):
            print(f"[backfill-pending] Rankings saved; unavailable details: {missing}. Keep the recording for later replay.")
        print(f"[saved] {version}\n[index] {index}\n[daily-selection] {'selected' if selected else 'retained historical version'}")
        print(f"Deploy only: {args.output.resolve()}\nPrivate capture and diagnostics remain local.")
        return 0
    except (ValueError, OSError, RuntimeError, EOFError, ZoneInfoNotFoundError) as exc:
        print(f"[not-imported] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
