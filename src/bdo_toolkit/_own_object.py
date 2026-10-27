"""The player's own object ID, shared by every decoder that needs to know "is this me".

Only messages the server sends about the player alone (profile key
``own_object``, e.g. the class resource change) may set the ID. Messages sent for every
nearby object, such as positions, only read it: a nearby movement cannot tell
the player from anyone else.
"""
from collections.abc import Callable

from ._protocol import BDOFrame, FlowKey
from .profiles import OwnObjectProfileLayout, OwnObjectSource

# Every player object ID seen so far is below this; mounts, NPCs and
# monsters start at 0x09000000. The resource message also reports nearby
# mounts, one of which this keeps from being taken for the player.
PLAYER_OBJECT_ID_LIMIT = 0x02000000

OwnObjectListener = Callable[[int | None], None]


class OwnObjectError(RuntimeError):
    """An own-object source no longer fits the traffic or named two players."""


def validate_object_id(object_id: int | None) -> int | None:
    if object_id is not None and (type(object_id) is not int or not 0 <= object_id <= 0xFFFFFFFF):
        raise ValueError("object_id must be a uint32 or None")
    return object_id


class OwnObjectTracker:
    """Learns, holds and forgets the player's object ID for one capture.

    Feed it every frame with ``observe``; read ``own_object_id`` or
    ``is_own``; ``subscribe`` to hear when the ID is learned or forgotten.
    The ID is new on every login, so it is forgotten when the connection it
    was learned on closes or starts a new epoch. A given ``object_id`` is
    fixed: never learned over, never forgotten.
    """

    def __init__(self, layout: OwnObjectProfileLayout | None, *, object_id: int | None = None) -> None:
        if layout is not None and not isinstance(layout, OwnObjectProfileLayout):
            raise TypeError("layout must be an OwnObjectProfileLayout or None")
        self._fixed = validate_object_id(object_id) is not None
        self._sources: dict[int, OwnObjectSource] = (
            {} if layout is None or self._fixed else {source.opcode: source for source in layout.sources})
        self._own_object_id = object_id
        self._connection: tuple[FlowKey, int] | None = None
        self._listeners: list[OwnObjectListener] = []

    @property
    def own_object_id(self) -> int | None:
        return self._own_object_id

    @property
    def can_identify(self) -> bool:
        """True when an ID is given or a source can reveal it."""
        return self._fixed or bool(self._sources)

    def is_own(self, object_id: int) -> bool:
        return self._own_object_id is not None and object_id == self._own_object_id

    def subscribe(self, listener: OwnObjectListener) -> None:
        self._listeners.append(listener)

    def observe(self, frame: BDOFrame) -> None:
        """Forget the ID after its connection resets; learn it from a source message."""
        connection = self._connection
        if connection is not None and connection[0] == frame.context.flow \
                and connection[1] != frame.context.flow_generation:
            self._set(None, None)
        source = self._sources.get(frame.opcode)
        if source is None:
            return
        if frame.flag != source.flag or frame.length != source.message_length \
                or len(frame.message) != source.message_length:
            raise OwnObjectError(f"Own-object message 0x{frame.opcode:04X} shape changed; "
                                 "update this patch's own_object layout")
        object_id = int.from_bytes(frame.message[source.object_id_offset:source.object_id_offset + 4], "little")
        if object_id >= PLAYER_OBJECT_ID_LIMIT:
            return
        connection = (frame.context.flow, frame.context.flow_generation)
        if object_id == self._own_object_id:
            self._connection = connection
            return
        if self._own_object_id is not None and self._connection == connection:
            raise OwnObjectError("Own-object messages named two players on one connection (party?); "
                                 "pass object_id")
        self._set(object_id, connection)

    def close_flow(self, flow: FlowKey) -> None:
        if self._connection is not None and self._connection[0] == flow:
            self._set(None, None)

    def _set(self, object_id: int | None, connection: tuple[FlowKey, int] | None) -> None:
        if self._fixed:
            return
        changed = object_id != self._own_object_id
        self._own_object_id = object_id
        self._connection = connection
        if changed:
            for listener in self._listeners:
                listener(object_id)
