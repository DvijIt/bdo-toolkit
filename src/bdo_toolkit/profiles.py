"""Opcode profile helpers."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Any, Optional


OPCODE_PROFILE_SCHEMA_VERSION = 1


class ProfileError(ValueError):
    """Raised when an opcode profile has an invalid JSON or schema shape."""


@dataclass(frozen=True)
class XPProfileLayout:
    """Solo-only numeric XP geometry, never a player identifier or balance.

    Level is a logical uint8 (supported levels 1..75); both XP counters are
    uint64 little endian. This does not assert the server's level storage width.
    """

    opcode: int
    message_length: int
    level_offset: int
    current_offset: int
    required_offset: int
    flag: int = 0
    encoding: str = "level-u8-xp-u64le-solo-v1"

    def __post_init__(self) -> None:
        for name, low, high in (("opcode", 0, 65535), ("message_length", 22, 4096),
                                ("level_offset", 5, 4095), ("current_offset", 5, 4088),
                                ("required_offset", 5, 4088), ("flag", 0, 0)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ProfileError(f"xp.{name} must be an integer from {low} to {high}")
        if self.encoding != "level-u8-xp-u64le-solo-v1":
            raise ProfileError("Unsupported xp encoding/scope")
        occupied: set[int] = set()
        for offset, width in ((self.level_offset, 1), (self.current_offset, 8), (self.required_offset, 8)):
            positions = set(range(offset, offset + width))
            if offset + width > self.message_length or occupied & positions:
                raise ProfileError("xp fields overlap or extend outside the message")
            occupied.update(positions)

    def to_dict(self) -> dict[str, Any]:
        return {"opcode": f"0x{self.opcode:04X}", "message_length": self.message_length,
                "level_offset": self.level_offset, "current_offset": self.current_offset,
                "required_offset": self.required_offset, "flag": self.flag, "encoding": self.encoding}


def _xp_layout(value: object) -> XPProfileLayout | None:
    if value is None:
        return None
    keys = {"opcode", "message_length", "level_offset", "current_offset", "required_offset", "flag", "encoding"}
    if not isinstance(value, dict) or value.keys() != keys:
        raise ProfileError("xp must contain exactly the solo numeric layout fields")
    return XPProfileLayout(**{**value, "opcode": _profile_opcode(value["opcode"], "xp.opcode")})


@dataclass(frozen=True)
class PositionSource:
    """One message that reports a nearby object's world position.

    Offsets count from the start of the message, header included. x, y
    (height), z are three consecutive f32 little endian in the game files'
    centimetres; the object ID is a uint32 little endian. The optional
    ``facing_offset`` names the facing direction: a unit vector of three f32
    (x, y, z) with y 0.
    """

    opcode: int
    message_length: int
    position_offset: int
    object_id_offset: int
    facing_offset: int | None = None
    flag: int = 0

    def __post_init__(self) -> None:
        for name, low, high in (("opcode", 0, 65535), ("message_length", 21, 4096),
                                ("position_offset", 5, 4084), ("object_id_offset", 5, 4092),
                                ("flag", 0, 0)):
            _check_layout_int(self, "position.sources[]", name, low, high)
        if self.facing_offset is not None:
            _check_layout_int(self, "position.sources[]", "facing_offset", 5, 4084)
        occupied: set[int] = set()
        for offset, width in ((self.position_offset, 12), (self.object_id_offset, 4), (self.facing_offset, 12)):
            if offset is None:
                continue
            positions = set(range(offset, offset + width))
            if offset + width > self.message_length or occupied & positions:
                raise ProfileError("position source fields overlap or extend outside the message")
            occupied.update(positions)

    def to_dict(self) -> dict[str, Any]:
        return {"opcode": f"0x{self.opcode:04X}", "message_length": self.message_length,
                "position_offset": self.position_offset, "object_id_offset": self.object_id_offset,
                "facing_offset": self.facing_offset, "flag": self.flag}


@dataclass(frozen=True)
class PositionProfileLayout:
    """The messages that report nearby objects' positions.

    Every source is sent for every moving object near the player, not only
    the player, and they complement each other: on client 3464 one reports
    changes of movement, the other steady updates (also while walking
    backwards, which the first leaves out).
    """

    sources: tuple[PositionSource, ...]
    encoding: str = "xyz-f32le-object-u32le-v2"

    def __post_init__(self) -> None:
        if not isinstance(self.sources, tuple) or not 1 <= len(self.sources) <= 16:
            raise ProfileError("position.sources must be a tuple of 1 to 16 sources")
        for source in self.sources:
            if not isinstance(source, PositionSource):
                raise ProfileError("position.sources must hold PositionSource values")
            source.__post_init__()
        if len({source.opcode for source in self.sources}) != len(self.sources):
            raise ProfileError("position.sources must have distinct opcodes")
        if self.encoding != "xyz-f32le-object-u32le-v2":
            raise ProfileError("Unsupported position encoding")

    def to_dict(self) -> dict[str, Any]:
        return {"sources": [source.to_dict() for source in self.sources], "encoding": self.encoding}


def _position_layout(value: object) -> PositionProfileLayout | None:
    if value is None:
        return None
    if (not isinstance(value, dict) or value.keys() != {"sources", "encoding"}
            or not isinstance(value["sources"], list)):
        raise ProfileError("position must contain exactly a sources list and an encoding")
    keys = {"opcode", "message_length", "position_offset", "object_id_offset", "facing_offset", "flag"}
    sources = []
    for entry in value["sources"]:
        if not isinstance(entry, dict) or entry.keys() != keys:
            raise ProfileError("each position source must contain exactly the source fields")
        sources.append(PositionSource(**{**entry, "opcode": _profile_opcode(entry["opcode"], "position.opcode")}))
    return PositionProfileLayout(tuple(sources), value["encoding"])


@dataclass(frozen=True)
class OwnObjectSource:
    """One message only the player receives about itself, and where it names the player.

    The offset counts from the start of the message, header included; the
    object ID is a uint32 little endian.
    """

    opcode: int
    message_length: int
    object_id_offset: int
    flag: int = 0

    def __post_init__(self) -> None:
        for name, low, high in (("opcode", 0, 65535), ("message_length", 9, 4096),
                                ("object_id_offset", 5, 4092), ("flag", 0, 0)):
            _check_layout_int(self, "own_object.sources[]", name, low, high)
        if self.object_id_offset + 4 > self.message_length:
            raise ProfileError("own_object source object ID extends outside the message")

    def to_dict(self) -> dict[str, Any]:
        return {"opcode": f"0x{self.opcode:04X}", "message_length": self.message_length,
                "object_id_offset": self.object_id_offset, "flag": self.flag}


@dataclass(frozen=True)
class OwnObjectProfileLayout:
    """Messages that reveal the player's object ID, which is new on every login."""

    sources: tuple[OwnObjectSource, ...]
    encoding: str = "object-u32le-v1"

    def __post_init__(self) -> None:
        if not isinstance(self.sources, tuple) or not 1 <= len(self.sources) <= 16:
            raise ProfileError("own_object.sources must be a tuple of 1 to 16 sources")
        for source in self.sources:
            if not isinstance(source, OwnObjectSource):
                raise ProfileError("own_object.sources must hold OwnObjectSource values")
            source.__post_init__()
        if len({source.opcode for source in self.sources}) != len(self.sources):
            raise ProfileError("own_object.sources must have distinct opcodes")
        if self.encoding != "object-u32le-v1":
            raise ProfileError("Unsupported own_object encoding")

    def to_dict(self) -> dict[str, Any]:
        return {"sources": [source.to_dict() for source in self.sources], "encoding": self.encoding}


def _own_object_layout(value: object) -> OwnObjectProfileLayout | None:
    if value is None:
        return None
    if (not isinstance(value, dict) or value.keys() != {"sources", "encoding"}
            or not isinstance(value["sources"], list)):
        raise ProfileError("own_object must contain exactly a sources list and an encoding")
    keys = {"opcode", "message_length", "object_id_offset", "flag"}
    sources = []
    for entry in value["sources"]:
        if not isinstance(entry, dict) or entry.keys() != keys:
            raise ProfileError("each own_object source must contain exactly the source fields")
        sources.append(OwnObjectSource(**{**entry, "opcode": _profile_opcode(entry["opcode"], "own_object.opcode")}))
    return OwnObjectProfileLayout(tuple(sources), value["encoding"])


@dataclass(frozen=True)
class ResourceProfileLayout:
    """Class resource change geometry: max, signed change, object ID, current.

    The resource is MP or the class's own kind; one message serves every class.

    Offsets count from the start of the message, header included; all four
    fields are 32-bit little endian, the change signed. The server sends it
    for the player and for nearby mounts (another stat, same shape).
    """

    opcode: int
    message_length: int
    maximum_offset: int
    change_offset: int
    object_id_offset: int
    current_offset: int
    flag: int = 0
    encoding: str = "resource-u32le-change-i32le-v1"

    def __post_init__(self) -> None:
        for name, low, high in (("opcode", 0, 65535), ("message_length", 21, 4096),
                                ("maximum_offset", 5, 4092), ("change_offset", 5, 4092),
                                ("object_id_offset", 5, 4092), ("current_offset", 5, 4092), ("flag", 0, 0)):
            _check_layout_int(self, "resource", name, low, high)
        if self.encoding != "resource-u32le-change-i32le-v1":
            raise ProfileError("Unsupported resource encoding")
        occupied: set[int] = set()
        for offset in (self.maximum_offset, self.change_offset, self.object_id_offset, self.current_offset):
            positions = set(range(offset, offset + 4))
            if offset + 4 > self.message_length or occupied & positions:
                raise ProfileError("resource fields overlap or extend outside the message")
            occupied.update(positions)

    def to_dict(self) -> dict[str, Any]:
        return {"opcode": f"0x{self.opcode:04X}", "message_length": self.message_length,
                "maximum_offset": self.maximum_offset, "change_offset": self.change_offset,
                "object_id_offset": self.object_id_offset, "current_offset": self.current_offset,
                "flag": self.flag, "encoding": self.encoding}


def _resource_layout(value: object) -> ResourceProfileLayout | None:
    if value is None:
        return None
    keys = {"opcode", "message_length", "maximum_offset", "change_offset", "object_id_offset",
            "current_offset", "flag", "encoding"}
    if not isinstance(value, dict) or value.keys() != keys:
        raise ProfileError("resource must contain exactly the resource layout fields")
    return ResourceProfileLayout(**{**value, "opcode": _profile_opcode(value["opcode"], "resource.opcode")})


def _check_layout_int(layout: object, section: str, name: str, low: int, high: int) -> None:
    value = getattr(layout, name)
    if type(value) is not int or not low <= value <= high:
        raise ProfileError(f"{section}.{name} must be an integer from {low} to {high}")


@dataclass(frozen=True)
class AgrisProfileLayout:
    """Portable Agris geometry; no player balance or connection identity."""

    opcode: int
    message_length: int
    remaining_offset: int
    maximum_offset: int
    flag: int = 0
    encoding: str = "uint32_le"
    observed_date: str | None = None

    def __post_init__(self) -> None:
        for name, low, high in (("opcode", 0, 65535), ("message_length", 13, 65535),
                                ("remaining_offset", 5, 65531), ("maximum_offset", 5, 65531),
                                ("flag", 0, 0)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ProfileError(f"agris.{name} must be an integer from {low} to {high}")
        if self.encoding != "uint32_le":
            raise ProfileError("agris.encoding must be uint32_le")
        if max(self.remaining_offset, self.maximum_offset) + 4 > self.message_length:
            raise ProfileError("agris offsets extend outside the message")
        if abs(self.remaining_offset - self.maximum_offset) < 4:
            raise ProfileError("agris remaining and maximum fields overlap")
        if self.observed_date is not None:
            try:
                if not isinstance(self.observed_date, str) or date.fromisoformat(self.observed_date).isoformat() != self.observed_date:
                    raise ValueError
            except ValueError as exc:
                raise ProfileError("agris.observed_date must be YYYY-MM-DD or null") from exc

    def to_dict(self) -> dict[str, Any]:
        return {"opcode": f"0x{self.opcode:04X}", "message_length": self.message_length,
                "remaining_offset": self.remaining_offset, "maximum_offset": self.maximum_offset,
                "flag": self.flag, "encoding": self.encoding, "observed_date": self.observed_date}


def _agris_layout(value: object) -> AgrisProfileLayout | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ProfileError("agris must be an object or null")
    required = {"opcode", "message_length", "remaining_offset", "maximum_offset", "flag", "encoding"}
    if not required <= value.keys() or value.keys() - required - {"observed_date"}:
        raise ProfileError("agris has missing or unsupported fields")
    return AgrisProfileLayout(opcode=_profile_opcode(value["opcode"], "agris.opcode"),
                              message_length=value["message_length"],
                              remaining_offset=value["remaining_offset"], maximum_offset=value["maximum_offset"],
                              flag=value["flag"], encoding=value["encoding"], observed_date=value.get("observed_date"))


@dataclass(frozen=True)
class OriginCompanionFamily:
    """One explicitly promoted structural family trusted by the classifier."""

    delta_opcode: int
    companion_opcodes: tuple[int, int]
    companion_lengths: tuple[int, int]
    detection: str
    observations: int
    promoted_at: Optional[str]

    @property
    def family_key(self) -> tuple[int, int, int, int, int]:
        return (
            self.delta_opcode,
            self.companion_opcodes[0],
            self.companion_opcodes[1],
            self.companion_lengths[0],
            self.companion_lengths[1],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "delta_opcode": f"0x{self.delta_opcode:04X}",
            "companion_opcodes": [
                f"0x{opcode:04X}" for opcode in self.companion_opcodes
            ],
            "companion_lengths": list(self.companion_lengths),
            "detection": self.detection,
            "observations": self.observations,
            "promoted_at": self.promoted_at,
        }


@dataclass(frozen=True)
class OpcodeProfile:
    """Versioned opcode profile metadata and raw specs."""

    path: Path
    active: bool
    version: int
    updated_at: Optional[str]
    calibration_item_id: Optional[int]
    specs: Mapping[str, tuple[Mapping[str, Any], ...]]
    origin_companion_families: tuple[OriginCompanionFamily, ...] = ()
    agris: AgrisProfileLayout | None = field(default=None, kw_only=True)
    xp: XPProfileLayout | None = field(default=None, kw_only=True)
    position: PositionProfileLayout | None = field(default=None, kw_only=True)
    own_object: OwnObjectProfileLayout | None = field(default=None, kw_only=True)
    resource: ResourceProfileLayout | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        if self.xp is not None:
            if not isinstance(self.xp, XPProfileLayout):
                raise ProfileError("xp must be an XPProfileLayout or None")
            self.xp.__post_init__()
        for name, layout_type in (("position", PositionProfileLayout), ("own_object", OwnObjectProfileLayout),
                                  ("resource", ResourceProfileLayout)):
            layout = getattr(self, name)
            if layout is not None:
                if not isinstance(layout, layout_type):
                    raise ProfileError(f"{name} must be a {layout_type.__name__} or None")
                layout.__post_init__()
        if self.agris is not None:
            if not isinstance(self.agris, AgrisProfileLayout):
                raise ProfileError("agris must be an AgrisProfileLayout or None")
            self.agris.__post_init__()
        object.__setattr__(
            self,
            "specs",
            MappingProxyType(
                {
                    event: tuple(
                        _freeze_profile_mapping(entry) for entry in entries
                    )
                    for event, entries in self.specs.items()
                }
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "active": self.active,
            "version": self.version,
            "updated_at": self.updated_at,
            "calibration_item_id": self.calibration_item_id,
            "specs": {
                event: [_thaw_profile_value(entry) for entry in entries]
                for event, entries in self.specs.items()
            },
            "origin_companion_families": [
                family.to_dict() for family in self.origin_companion_families
            ],
            **({"agris": self.agris.to_dict()} if self.agris is not None else {}),
            **({"xp": self.xp.to_dict()} if self.xp is not None else {}),
            **({"position": self.position.to_dict()} if self.position is not None else {}),
            **({"own_object": self.own_object.to_dict()} if self.own_object is not None else {}),
            **({"resource": self.resource.to_dict()} if self.resource is not None else {}),
        }


def load_opcode_profile(path: str | Path) -> OpcodeProfile:
    """Load and validate one explicit, app-owned opcode profile."""

    profile_path = Path(path)
    if not profile_path.is_file():
        raise FileNotFoundError(f"Opcode profile does not exist: {profile_path}")
    try:
        data = json.loads(profile_path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, RecursionError, UnicodeError) as exc:
        raise ProfileError(f"Could not parse opcodes JSON {profile_path}: {exc}") from exc
    return _opcode_profile_from_data(data, profile_path)


def _opcode_profile_from_data(data: Any, profile_path: Path) -> OpcodeProfile:
    """Shared validation for loaded and proposed profile content."""
    if not isinstance(data, dict):
        raise ProfileError(f"Opcodes JSON {profile_path} must be a top-level object")

    active_value = data.get("profile_active", False)
    if not isinstance(active_value, bool):
        raise ProfileError(f"profile_active in {profile_path} must be a boolean")

    version_value = data.get("version")
    if (
        isinstance(version_value, bool)
        or not isinstance(version_value, int)
        or version_value != OPCODE_PROFILE_SCHEMA_VERSION
    ):
        raise ProfileError(
            f"version in {profile_path} must be "
            f"{OPCODE_PROFILE_SCHEMA_VERSION}"
        )

    updated_at_value = data.get("updated_at")
    if updated_at_value is not None and not isinstance(updated_at_value, str):
        raise ProfileError(f"updated_at in {profile_path} must be a string")

    calibration_item_value = data.get("calibration_item_id")
    if calibration_item_value is not None and (
        isinstance(calibration_item_value, bool)
        or not isinstance(calibration_item_value, int)
        or not 1 <= calibration_item_value <= 0xFFFFFFFF
    ):
        raise ProfileError(
            f"calibration_item_id in {profile_path} must be a positive uint32"
        )

    specs = data.get("specs", {})
    if not isinstance(specs, dict):
        raise ProfileError(f"specs in {profile_path} must be an object")
    normalized_specs: dict[str, list[dict[str, Any]]] = {}
    for event_name, entries in specs.items():
        if not isinstance(event_name, str):
            raise ProfileError(f"spec event names in {profile_path} must be strings")
        if not isinstance(entries, list):
            raise ProfileError(
                f"specs[{event_name!r}] in {profile_path} must be a list"
            )
        if any(not isinstance(entry, dict) for entry in entries):
            raise ProfileError(
                f"every specs[{event_name!r}] entry in {profile_path} must be an object"
            )
        normalized_entries: list[dict[str, Any]] = []
        for index, entry in enumerate(entries):
            normalized = dict(entry)
            _validate_profile_entry(profile_path, event_name, index, normalized)
            normalized_entries.append(normalized)
        normalized_specs[event_name] = normalized_entries

    raw_families = data.get("origin_companion_families", [])
    if not isinstance(raw_families, list):
        raise ProfileError(
            f"origin_companion_families in {profile_path} must be a list"
        )
    families = tuple(
        _origin_companion_family(profile_path, index, entry)
        for index, entry in enumerate(raw_families)
    )
    immutable_specs = MappingProxyType(
        {
            event: tuple(_freeze_profile_mapping(entry) for entry in entries)
            for event, entries in normalized_specs.items()
        }
    )
    return OpcodeProfile(
        path=profile_path,
        active=active_value,
        version=version_value,
        updated_at=updated_at_value,
        calibration_item_id=calibration_item_value,
        specs=immutable_specs,
        origin_companion_families=families,
        agris=_agris_layout(data.get("agris")),
        xp=_xp_layout(data.get("xp")),
        position=_position_layout(data.get("position")),
        own_object=_own_object_layout(data.get("own_object")),
        resource=_resource_layout(data.get("resource")),
    )


def _freeze_profile_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(
        {key: _freeze_profile_value(item) for key, item in value.items()}
    )


def _freeze_profile_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _freeze_profile_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_profile_value(item) for item in value)
    return value


def _thaw_profile_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _thaw_profile_value(item) for key, item in value.items()
        }
    if isinstance(value, tuple):
        return [_thaw_profile_value(item) for item in value]
    return value


def _profile_opcode(value: object, location: str) -> int:
    if isinstance(value, bool):
        raise ProfileError(f"{location} must be a uint16")
    if isinstance(value, int):
        opcode = value
    elif isinstance(value, str):
        try:
            opcode = int(value, 16 if value.lower().startswith("0x") else 10)
        except ValueError as exc:
            raise ProfileError(f"{location} must be a uint16") from exc
    else:
        raise ProfileError(f"{location} must be a uint16")
    if not 0 <= opcode <= 0xFFFF:
        raise ProfileError(f"{location} must be a uint16")
    return opcode


def _origin_companion_family(
    path: Path,
    index: int,
    value: object,
) -> OriginCompanionFamily:
    location = f"origin_companion_families[{index}] in {path}"
    if not isinstance(value, dict):
        raise ProfileError(f"{location} must be an object")
    raw_opcodes = value.get("companion_opcodes")
    raw_lengths = value.get("companion_lengths")
    if not isinstance(raw_opcodes, list) or len(raw_opcodes) != 2:
        raise ProfileError(f"companion_opcodes in {location} must contain two values")
    if not isinstance(raw_lengths, list) or len(raw_lengths) != 2:
        raise ProfileError(f"companion_lengths in {location} must contain two values")
    lengths: list[int] = []
    for raw_length in raw_lengths:
        if (
            isinstance(raw_length, bool)
            or not isinstance(raw_length, int)
            or not 5 <= raw_length <= 0xFFFF
        ):
            raise ProfileError(
                f"companion_lengths in {location} must contain values from 5 to 65535"
            )
        lengths.append(raw_length)
    detection = value.get("detection")
    if detection != "shared-token-chain-v1":
        raise ProfileError(
            f"detection in {location} must be 'shared-token-chain-v1'"
        )
    observations = value.get("observations")
    if (
        isinstance(observations, bool)
        or not isinstance(observations, int)
        or observations <= 0
    ):
        raise ProfileError(f"observations in {location} must be a positive integer")
    promoted_at = value.get("promoted_at")
    if promoted_at is not None and not isinstance(promoted_at, str):
        raise ProfileError(f"promoted_at in {location} must be a string or null")
    return OriginCompanionFamily(
        delta_opcode=_profile_opcode(value.get("delta_opcode"), f"delta_opcode in {location}"),
        companion_opcodes=(
            _profile_opcode(raw_opcodes[0], f"companion_opcodes in {location}"),
            _profile_opcode(raw_opcodes[1], f"companion_opcodes in {location}"),
        ),
        companion_lengths=(lengths[0], lengths[1]),
        detection=detection,
        observations=observations,
        promoted_at=promoted_at,
    )


def _validate_profile_entry(
    path: Path,
    event: str,
    index: int,
    entry: dict[str, Any],
) -> None:
    location = f"specs[{event!r}][{index}] in {path}"
    embedded_event = entry.get("event")
    if embedded_event is not None and embedded_event != event:
        raise ProfileError(
            f"event in {location} must match its containing {event!r} category"
        )
    opcode = entry.get("opcode")
    if isinstance(opcode, bool):
        raise ProfileError(f"opcode in {location} must be a uint16")
    if isinstance(opcode, str):
        try:
            opcode_number = int(opcode, 16 if opcode.lower().startswith("0x") else 10)
        except ValueError as exc:
            raise ProfileError(f"opcode in {location} must be a uint16") from exc
    elif isinstance(opcode, int):
        opcode_number = opcode
    else:
        raise ProfileError(f"opcode in {location} must be a uint16")
    if not 0 <= opcode_number <= 0xFFFF:
        raise ProfileError(f"opcode in {location} must be a uint16")

    length = entry.get("length")
    if length is not None and (
        isinstance(length, bool)
        or not isinstance(length, int)
        or not 5 <= length <= 0xFFFF
    ):
        raise ProfileError(f"length in {location} must be 5..65535 or null")

    offset_fields = (
        "item_id_offset",
        "quantity_offset",
        "item_instance_offset",
        "context_offset",
        "record_count_offset",
        "inventory_slot_offset",
        "source_instance_offset",
        "quantity_removed_offset",
        "quantity_added_offset",
        "destination_instance_offset",
    )
    for field_name in offset_fields:
        value = entry.get(field_name)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise ProfileError(
                f"{field_name} in {location} must be a non-negative integer or null"
            )
    if event == "STORAGE_ITEM_DELTA":
        item_offset = entry.get("item_id_offset")
        context_offset = entry.get("context_offset")
        count_offset = entry.get("record_count_offset")
        if isinstance(item_offset, int) and not isinstance(item_offset, bool):
            if context_offset is not None and context_offset + 4 > item_offset:
                raise ProfileError(
                    f"context_offset in {location} must end before item_id_offset"
                )
            if count_offset is not None and count_offset + 2 > item_offset:
                raise ProfileError(
                    f"record_count_offset in {location} must end before item_id_offset"
                )
    repeat_stride = entry.get("repeat_stride")
    if repeat_stride is not None and (
        isinstance(repeat_stride, bool)
        or not isinstance(repeat_stride, int)
        or repeat_stride <= 0
    ):
        raise ProfileError(
            f"repeat_stride in {location} must be a positive integer or null"
        )
    for field_name in ("confidence", "source", "observed_at"):
        value = entry.get(field_name)
        if value is not None and not isinstance(value, str):
            raise ProfileError(f"{field_name} in {location} must be a string or null")
    score = entry.get("score")
    if score is not None and (
        isinstance(score, bool)
        or not isinstance(score, (int, float))
        or not math.isfinite(score)
        or not 0 <= score <= 1
    ):
        raise ProfileError(f"score in {location} must be a finite number from 0 to 1")
