"""Shared pieces for re-finding fixed-length messages that moved after a patch.

A patch can move a message's opcode, change its length and reorder its
fields, so the collector keeps every plain message of a range of lengths
and the caller's checks find the layout again; a caller accepts exactly one
candidate or none.
"""
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any, NoReturn

from ._capture_options import LiveCaptureOptions
from ._profile_io import atomic_write_text, next_backup_path
from ._profile_runtime import validate_runtime_profile
from ._protocol import BDOFrame, FlowKey
from ._tracked_capture import CaptureHealth, TrackedLiveSession, TrackedReplay
from .profiles import ProfileError, _opcode_profile_from_data

# Bounds on what one calibration capture may hold (a few minutes of play).
MAX_CALIBRATION_MESSAGES = 200_000


class LayoutRelocationError(RuntimeError):
    """A previous layout could not be re-found; no profile was written."""


@dataclass(frozen=True)
class RelocationStatus:
    """``collecting`` while candidate messages are kept; ``invalid`` once a bound was hit."""

    status: str
    candidate_messages: int
    reason: str | None = None


@dataclass(frozen=True)
class LayoutProfileUpdate:
    path: Path
    written: bool
    backup_path: Path | None

    def to_dict(self) -> dict[str, object]:
        return {"path": str(self.path), "written": self.written,
                "backup_path": str(self.backup_path) if self.backup_path else None}


class FixedLengthCollector:
    """Keeps the plain server messages of the wanted lengths, by opcode.

    An opcode seen with any other length cannot be a fixed-length message of
    one of those lengths, so it is no candidate.
    """

    def __init__(self, lengths: Iterable[int]) -> None:
        self._lengths = frozenset(lengths)
        self._frames: dict[int, list[BDOFrame]] = defaultdict(list)
        self._seen_lengths: dict[int, set[int]] = defaultdict(set)
        self._kept = 0
        self.reason: str | None = None

    def snapshot(self) -> RelocationStatus:
        return RelocationStatus("invalid" if self.reason else "collecting", self._kept, self.reason)

    def failed_snapshot(self, reason: str) -> RelocationStatus:
        return RelocationStatus("invalid", self._kept, reason)

    def invalidate(self, reason: str) -> NoReturn:
        self.reason = self.reason or reason
        raise LayoutRelocationError(self.reason)

    def observe(self, frame: BDOFrame) -> None:
        if self.reason:
            raise LayoutRelocationError(self.reason)
        if frame.flag != 0:
            return
        length = len(frame.message)
        self._seen_lengths[frame.opcode].add(length)
        if length in self._lengths:
            self._kept += 1
            if self._kept > MAX_CALIBRATION_MESSAGES:
                self.invalidate("Calibration capture holds too many candidate messages; use a shorter capture")
            self._frames[frame.opcode].append(frame)

    def close_flow(self, flow: FlowKey) -> None:
        pass  # Candidates are judged over the whole capture.

    @property
    def kept(self) -> int:
        return self._kept

    def fixed_length_candidates(self) -> dict[int, list[BDOFrame]]:
        """Opcodes sent at one of the wanted lengths and no other, with their messages."""
        return {opcode: frames for opcode, frames in self._frames.items()
                if len(self._seen_lengths[opcode]) == 1}


def collect_replay(path: str | Path, collector: FixedLengthCollector, *, ports: Iterable[int],
                   label: str) -> CaptureHealth:
    with TrackedReplay[None, RelocationStatus](path, ports=ports, label=label,
                                               make_tracker=lambda _publish: collector) as replay:
        for _ in replay:
            pass
        return replay.health


def collect_live(collector: FixedLengthCollector, *, capture_seconds: float, label: str,
                 live_options: LiveCaptureOptions | None, save_pcap: str | Path | None) -> CaptureHealth:
    if isinstance(capture_seconds, bool) or not isinstance(capture_seconds, int | float) or capture_seconds <= 0:
        raise ValueError("capture_seconds must be a positive number")
    session = TrackedLiveSession[None, RelocationStatus](
        label=label, make_tracker=lambda _publish: collector, live_options=live_options,
        capture_seconds=capture_seconds, save_pcap=save_pcap)
    with session:
        while not session.stopped:
            session.poll(timeout=0.25)
    session.raise_if_failed()
    return session.health


def require_clean(health: CaptureHealth) -> None:
    if not isinstance(health, CaptureHealth) or not health.capture_is_clean:
        raise LayoutRelocationError("Calibration requires a clean, completed capture; capture again")


def update_profile_sections(path: str | Path, sections: dict[str, Any], previous: dict[str, Any], *,
                            backup: bool, expected_profile_sha256: str | None) -> LayoutProfileUpdate:
    """Replace only the named sections of an existing active profile, with backup and atomic replace.

    Each section must still hold its ``previous`` layout (or already the new
    one: then nothing is written). A layout of None leaves its section out.
    """
    if not isinstance(backup, bool):
        raise TypeError("backup must be bool")
    profile_path = Path(path)
    original = profile_path.read_bytes()
    if expected_profile_sha256 is not None and hashlib.sha256(original).hexdigest() != expected_profile_sha256:
        raise ProfileError("Profile changed during calibration; no profile was written")
    data = json.loads(original.decode("utf-8-sig"))
    existing = _opcode_profile_from_data(data, profile_path)
    validate_runtime_profile(existing)
    if all(getattr(existing, name) == layout for name, layout in sections.items()):
        return LayoutProfileUpdate(profile_path, False, None)
    for name, layout in previous.items():
        if getattr(existing, name) != layout:
            raise ProfileError(f"Profile's {name} layout is not the one this calibration started from")
    for name, layout in sections.items():
        if layout is None:
            data.pop(name, None)
        else:
            data[name] = layout.to_dict()
    validate_runtime_profile(_opcode_profile_from_data(data, profile_path))
    if profile_path.read_bytes() != original:
        raise ProfileError("Profile changed before save; no profile was written")
    backup_path = next_backup_path(profile_path) if backup else None
    if backup_path is not None:
        shutil.copy2(profile_path, backup_path)
    atomic_write_text(profile_path, json.dumps(data, indent=2, sort_keys=True) + "\n")
    return LayoutProfileUpdate(profile_path, True, backup_path)
