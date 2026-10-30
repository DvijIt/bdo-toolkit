"""Immutable account records decoded from server messages, plus confirmed id tables."""
from dataclasses import asdict, dataclass, field
from typing import ClassVar

# Ids confirmed against the in-game UI (EU, Oct 2026); unknown ids render as "<kind>#<id>".
CLASS_NAMES: dict[int, str] = {
    2: "Ranger", 8: "Wukong", 13: "Corsair", 18: "Shai", 33: "Seraph", 34: "Dosa", 35: "Deadeye",
}
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
# Slots named only where every observed item agrees; 21, 22, 23 (costume face/ear accessories),
# 40 (a fishing rod) and 45 (a snowboard) are seen but left unnamed.
EQUIPMENT_SLOTS: dict[int, str] = {
    0: "main weapon", 1: "sub-weapon", 3: "armor", 4: "gloves", 5: "shoes", 6: "helmet", 7: "necklace",
    8: "ring", 9: "ring", 10: "earring", 11: "earring", 12: "belt", 14: "costume armor", 15: "costume gloves",
    16: "costume shoes", 17: "costume helmet", 18: "costume main weapon", 19: "costume sub-weapon",
    27: "alchemy stone", 29: "awakening weapon", 30: "costume awakening weapon", 31: "tome", 32: "artifact",
    33: "artifact",
}
LIFE_EQUIPMENT_SLOTS: dict[int, str] = {
    0: "lumbering axe", 1: "fluid collector", 2: "hoe", 3: "butcher knife", 4: "tanning knife", 5: "pickaxe",
    7: "artifact", 8: "artifact", 9: "fishing clothes", 10: "fishing tool", 11: "artifact", 12: "artifact",
    13: "hunting clothes", 14: "hunting tool", 17: "cooking clothes", 18: "cooking tool", 21: "alchemy clothes",
    22: "alchemy tool", 25: "processing clothes", 26: "processing tool", 29: "training clothes",
    30: "training tool", 33: "trade clothes", 36: "sailing clothes", 37: "sailing tool", 46: "necklace",
    47: "belt", 48: "ring", 49: "ring", 50: "earring", 51: "earring", 52: "alchemy stone",
}


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
    """The logged-in character, sent once on entering the world. The fame fields are the
    family's fame, shared by every character, not the character's own. ``slot`` is the
    character's position on the character select screen, starting at 0."""

    record_type: ClassVar[str] = "character_info"
    character_id: int
    nick: str
    family: str
    class_id: int
    fame_combat: int
    fame_life: int
    fame_third: int
    slot: int = 0

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
    """Short profile of the logged-in character. It carries a level-like value that lagged
    the real level after a level-up, so the level comes only from ``Experience``."""

    record_type: ClassVar[str] = "character_summary"
    character_id: int
    nick: str


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
    """One equipped item; ``item_id`` is the raw game id with the enhancement level in its top
    byte (as in ``split_item_id_enhancement``), names come from an item database."""

    slot: int
    item_id: int
    count: int
    durability: int
    max_durability: int
    instance_id: int

    @property
    def base_item_id(self) -> int:
        return self.item_id & 0xFFFFFF

    @property
    def enhancement_level(self) -> int:
        """0 when unenhanced. Its in-game label depends on the item: 16-20 are PRI-PEN on
        weapons and armor, while accessories count 1-5 as PRI-PEN."""
        return self.item_id >> 24


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
class Equipment(AccountRecord):
    """Combat gear and costumes worn by the logged-in character, sent once on entering the
    world right after ``CharacterInfo``. ``character_id`` comes from that ``CharacterInfo``
    (the message itself names no character) and is None if it was not seen. ``slot`` is
    the game's equipment slot (``EQUIPMENT_SLOTS``)."""

    record_type: ClassVar[str] = "equipment"
    character_id: int | None
    gear: tuple[GearItem, ...] = field(default=())

    @property
    def slot_names(self) -> tuple[str, ...]:
        return tuple(_name(EQUIPMENT_SLOTS, "slot", g.slot) for g in self.gear)


@dataclass(frozen=True)
class LifeEquipment(AccountRecord):
    """Life skill gear (tools, outfits, life accessories and artifacts), sent once on
    entering the world. It is shared by the family: every character received the same set.
    ``slot`` keys ``LIFE_EQUIPMENT_SLOTS``."""

    record_type: ClassVar[str] = "life_equipment"
    gear: tuple[GearItem, ...] = field(default=())

    @property
    def slot_names(self) -> tuple[str, ...]:
        return tuple(_name(LIFE_EQUIPMENT_SLOTS, "slot", g.slot) for g in self.gear)


@dataclass(frozen=True)
class KnowledgeEntry:
    """One gained knowledge. ``level`` grades confirmed in game: 1 C, 2 B, 3 A, 5 S (4 is
    unconfirmed); knowledge without a grade reports 1."""

    knowledge_id: int
    level: int


@dataclass(frozen=True)
class KnowledgeBatch(AccountRecord):
    """Part of the family knowledge book, sent on entering the world in several messages.

    The batches together list every gained knowledge; ``ecology_points`` repeats in each.
    """

    record_type: ClassVar[str] = "knowledge_batch"
    ecology_points: int
    entries: tuple[KnowledgeEntry, ...] = field(default=())


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


@dataclass(frozen=True)
class Contribution(AccountRecord):
    """Family contribution points, sent on entering the world: ``available`` of ``total``
    are free to invest (the in-game "195/411"). ``experience`` grows towards the next point
    and restarts when one is gained."""

    record_type: ClassVar[str] = "contribution"
    available: int
    total: int
    experience: int


@dataclass(frozen=True)
class SkillPoints(AccountRecord):
    """Skill points of the logged-in character, sent with combat XP: ``available`` of
    ``total`` (the in-game "183/1892")."""

    record_type: ClassVar[str] = "skill_points"
    available: int
    total: int


@dataclass(frozen=True)
class NodeState:
    """One node (waypoint key, as in worker towns); ``invested`` means contribution points
    are invested in it."""

    node_id: int
    invested: bool


@dataclass(frozen=True)
class NodeBatch(AccountRecord):
    """Part of the family node list, sent on entering the world in two messages that
    together cover every node."""

    record_type: ClassVar[str] = "node_batch"
    nodes: tuple[NodeState, ...] = field(default=())

    @property
    def invested(self) -> tuple[int, ...]:
        return tuple(n.node_id for n in self.nodes if n.invested)


@dataclass(frozen=True)
class QuestKey:
    """A quest as the client keys it: ``group`` and ``index`` ("40022-14")."""

    group: int
    index: int


@dataclass(frozen=True)
class CompletedQuests(AccountRecord):
    """Part of the completed-quest list of the logged-in character, sent in several
    messages on entering the world. It holds family quests too, but not every completed
    quest: the in-game counter is higher than the union over all characters."""

    record_type: ClassVar[str] = "completed_quests"
    quests: tuple[QuestKey, ...] = field(default=())


@dataclass(frozen=True)
class OpenQuests(AccountRecord):
    """Part of a quest list of the logged-in character with no overlap with the completed
    ones; a handed-in quest leaves it. Probably quests that are unlocked but not done
    (unconfirmed), sent in several messages on entering the world."""

    record_type: ClassVar[str] = "open_quests"
    quests: tuple[QuestKey, ...] = field(default=())
