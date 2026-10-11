"""Re-find the combat opcodes after a patch and update a profile explicitly.

Relocation keeps the previous layout's record shapes and field offsets and
searches only the opcodes (see ``_relocation``). Capture a couple of minutes
of fighting that walks into view of monsters, kills several, and passes
their corpses again later. Nothing is written until ``update_combat_profile``.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import shutil

from .._capture_options import LiveCaptureOptions
from .._profile_io import atomic_write_text, next_backup_path
from .._profile_runtime import validate_runtime_profile
from .._protocol import DEFAULT_SERVER_PORTS
from .._tracked_capture import CaptureHealth, TrackedLiveSession, TrackedReplay
from ..profiles import CombatProfileLayout, OpcodeProfile, ProfileError, _opcode_profile_from_data
from ._relocation import (CombatCalibrationError, CombatRelocationEvidence, CombatRelocationStatus,
                          RelocationCollector, relocate)
from ._tracker import profile_layout


@dataclass(frozen=True)
class CombatCalibrationResult:
    """A relocated combat layout from a clean capture, with the evidence for it."""

    layout: CombatProfileLayout
    previous: CombatProfileLayout
    evidence: CombatRelocationEvidence
    candidate_messages: int
    health: CaptureHealth
    completed_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.layout, CombatProfileLayout) or not isinstance(self.previous, CombatProfileLayout):
            raise TypeError("layout and previous must be CombatProfileLayout")
        self.layout.__post_init__()
        if not isinstance(self.health, CaptureHealth) or not self.health.capture_is_clean:
            raise CombatCalibrationError("Calibration requires a clean, completed capture")

    @property
    def changed(self) -> bool:
        return self.layout != self.previous

    def to_dict(self) -> dict[str, object]:
        return {"layout": self.layout.to_dict(), "previous": self.previous.to_dict(), "changed": self.changed,
                "evidence": self.evidence.to_dict(), "candidate_messages": self.candidate_messages,
                "health": self.health.to_dict(), "completed_at": self.completed_at}


@dataclass(frozen=True)
class CombatProfileUpdate:
    path: Path
    written: bool
    backup_path: Path | None
    layout: CombatProfileLayout

    def to_dict(self) -> dict[str, object]:
        return {"path": str(self.path), "written": self.written,
                "backup_path": str(self.backup_path) if self.backup_path else None, "layout": self.layout.to_dict()}


def _result(collector: RelocationCollector, health: CaptureHealth) -> CombatCalibrationResult:
    if not health.capture_is_clean:
        raise CombatCalibrationError("Calibration capture lost packets; capture again")
    layout, evidence = relocate(collector)
    return CombatCalibrationResult(layout, collector.previous, evidence, len(collector.frames), health,
                                   datetime.now(UTC).isoformat())


def relocate_combat_replay(path: str | Path, *, profile: OpcodeProfile,
                           ports: Iterable[int] = DEFAULT_SERVER_PORTS) -> CombatCalibrationResult:
    """Re-find the profile's combat opcodes in a saved capture; never writes a file."""
    previous = profile_layout(profile)
    collectors: list[RelocationCollector] = []

    def make(_publish: object) -> RelocationCollector:
        collectors.append(RelocationCollector(previous))
        return collectors[-1]

    with TrackedReplay[None, CombatRelocationStatus](path, ports=ports, label="Combat calibration",
                                                    make_tracker=make) as replay:
        for _ in replay:
            pass
        health = replay.health
    return _result(collectors[0], health)


def relocate_combat_live(*, profile: OpcodeProfile, capture_seconds: float,
                         live_options: LiveCaptureOptions | None = None,
                         save_pcap: str | Path | None = None) -> CombatCalibrationResult:
    """Capture for ``capture_seconds``, then re-find the combat opcodes; never writes a file."""
    if isinstance(capture_seconds, bool) or not isinstance(capture_seconds, int | float) or capture_seconds <= 0:
        raise ValueError("capture_seconds must be a positive number")
    previous = profile_layout(profile)
    collector = RelocationCollector(previous)
    session = TrackedLiveSession[None, CombatRelocationStatus](
        label="Combat calibration", make_tracker=lambda _publish: collector, live_options=live_options,
        capture_seconds=capture_seconds, save_pcap=save_pcap)
    with session:
        while not session.stopped:
            session.poll(timeout=0.25)
    session.raise_if_failed()
    return _result(collector, session.health)


def update_combat_profile(result: CombatCalibrationResult, path: str | Path, *, backup: bool = True,
                          expected_profile_sha256: str | None = None) -> CombatProfileUpdate:
    """Replace only the combat layout of an existing active profile, with backup and atomic replace.

    An unchanged layout is a no-op. Refuses a profile that changed since
    ``expected_profile_sha256`` was taken or that would not validate.
    """
    if not isinstance(result, CombatCalibrationResult):
        raise TypeError("result must be a CombatCalibrationResult")
    result.__post_init__()
    if not isinstance(backup, bool):
        raise TypeError("backup must be bool")
    profile_path = Path(path)
    original = profile_path.read_bytes()
    if expected_profile_sha256 is not None and hashlib.sha256(original).hexdigest() != expected_profile_sha256:
        raise ProfileError("Profile changed during combat calibration; no profile was written")
    data = json.loads(original.decode("utf-8-sig"))
    existing = _opcode_profile_from_data(data, profile_path)
    validate_runtime_profile(existing)
    if existing.combat == result.layout:
        return CombatProfileUpdate(profile_path, False, None, result.layout)
    if existing.combat != result.previous:
        raise ProfileError("Profile's combat layout is not the one this calibration started from")
    data["combat"] = result.layout.to_dict()
    validate_runtime_profile(_opcode_profile_from_data(data, profile_path))
    if profile_path.read_bytes() != original:
        raise ProfileError("Profile changed before combat save; no profile was written")
    backup_path = next_backup_path(profile_path) if backup else None
    if backup_path is not None:
        shutil.copy2(profile_path, backup_path)
    atomic_write_text(profile_path, json.dumps(data, indent=2, sort_keys=True) + "\n")
    return CombatProfileUpdate(profile_path, True, backup_path, result.layout)
