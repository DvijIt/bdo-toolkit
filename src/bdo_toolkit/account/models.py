"""Immutable account records decoded from server messages, plus confirmed id tables."""
from dataclasses import asdict, dataclass, field
from typing import ClassVar

# Ids confirmed against the in-game UI (EU, Oct 2026); unknown ids render as "<kind>#<id>".
CLASS_NAMES: dict[int, str] = {34: "Dosa"}
WORKER_TYPES: dict[int, str] = {
    7552: "Skilled Goblin Worker",
    7562: "Professional Goblin Worker",
    7572: "Artisan Goblin Worker",
    7573: "Artisan Human Worker",
    8049: "Professional Dokkebi Worker",
}
TOWNS: dict[int, str] = {
    301: "Heidel", 302: "Glish", 602: "Keplan", 604: "Port Epheria", 608: "Trent",
    1141: "Tarif", 1781: "Dalbeol Village",
}
WORKER_SKILLS: dict[int, str] = {
    1002: "Mass Production Knowledge", 1004: "Tool Knowledge", 1010: "Mount Knowledge",
    1201: "Thrifty A", 1403: "Work Supervisor C", 1501: "Lucky Guy A", 1503: "Lucky Guy C",
    1603: "Wings C", 1902: "Craftsmanship", 1909: "Ore Packing",
}
SERVANT_CATEGORIES: dict[int, str] = {0: "horse", 1: "ship", 3: "camp"}
CHAT_CHANNELS: dict[int, str] = {12: "recruitment", 19: "world"}


def _name(table: dict[int, str], kind: str, key: int) -> str:
    return table.get(key, f"{kind}#{key}")


@dataclass(frozen=True)
class AccountRecord:
    """Base of every decoded record; ``observed_at`` is the message capture time."""

    record_type: ClassVar[str] = "account_record"
    observed_at: float

    def to_dict(self) -> dict[str, object]:
        return {"schema_version": 1, "record_type": self.record_type, **asdict(self)}


@dataclass(frozen=True)
class CharacterInfo(AccountRecord):
    """The logged-in character, sent once on entering the world."""

    record_type: ClassVar[str] = "character_info"
    character_id: int
    nick: str
    family: str
    class_id: int
    fame_combat: int
    fame_life: int
    fame_third: int

    @property
    def class_name(self) -> str:
        return _name(CLASS_NAMES, "class", self.class_id)


@dataclass(frozen=True)
class Experience(AccountRecord):
    """Level and XP of the logged-in character; sent on load and on every change."""

    record_type: ClassVar[str] = "experience"
    level: int
    xp: int
    xp_to_next: int

    @property
    def percent(self) -> float:
        return 100 * self.xp / self.xp_to_next if self.xp_to_next else 0.0


@dataclass(frozen=True)
class CharacterSummary(AccountRecord):
    """Short profile of the logged-in character."""

    record_type: ClassVar[str] = "character_summary"
    character_id: int
    nick: str
    level: int


@dataclass(frozen=True)
class PlayerSighting(AccountRecord):
    """Another player from the server's player list; ``level`` 0 means it was not sent."""

    record_type: ClassVar[str] = "player_sighting"
    character_id: int
    nick: str
    family: str
    level: int
    class_id: int

    @property
    def class_name(self) -> str:
        return _name(CLASS_NAMES, "class", self.class_id)


@dataclass(frozen=True)
class ChatMessage(AccountRecord):
    record_type: ClassVar[str] = "chat_message"
    channel: int
    family: str
    nick: str
    text: str

    @property
    def channel_name(self) -> str:
        return _name(CHAT_CHANNELS, "channel", self.channel)


@dataclass(frozen=True)
class GearItem:
    """One equipped item; ``item_id`` is the raw game id, names come from an item database."""

    slot: int
    item_id: int
    count: int
    durability: int
    max_durability: int
    instance_id: int


@dataclass(frozen=True)
class Servant(AccountRecord):
    """A family mount, ship or camp with its equipped gear. ``level_guess`` is unconfirmed."""

    record_type: ClassVar[str] = "servant"
    kind_id: int
    name: str
    category: int
    level_guess: int
    gear: tuple[GearItem, ...] = field(default=())

    @property
    def category_name(self) -> str:
        return _name(SERVANT_CATEGORIES, "category", self.category)


@dataclass(frozen=True)
class Worker(AccountRecord):
    """One worker. Work speed, movement speed and luck are computed by the client and not sent."""

    record_type: ClassVar[str] = "worker"
    worker_id: int
    type_id: int
    level: int
    stamina: int
    town_id: int
    skills: tuple[int, ...] = field(default=())

    @property
    def type_name(self) -> str:
        return _name(WORKER_TYPES, "worker", self.type_id)

    @property
    def town_name(self) -> str:
        return _name(TOWNS, "town", self.town_id)

    @property
    def skill_names(self) -> tuple[str, ...]:
        return tuple(_name(WORKER_SKILLS, "skill", s) for s in self.skills)
