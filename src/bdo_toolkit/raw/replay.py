"""Streaming offline raw-message replay; no opcode profile is involved."""
from collections.abc import Iterable
from pathlib import Path

from .._protocol import DEFAULT_SERVER_PORTS
from .._tracked_capture import TrackedReplay
from ._collector import RawTracker
from .models import RawMessage

# Messages one packet may release at once, e.g. when a held-back gap fills;
# raw traffic is far denser than decoded readings.
MAX_PENDING_MESSAGES = 65536

RawReplay = TrackedReplay[RawMessage, None]


def replay_raw(path: str | Path, *, ports: Iterable[int] = DEFAULT_SERVER_PORTS) -> RawReplay:
    """Every server-to-client message in a saved .pcap/.pcapng, in stream order.

    Bounded single-consumer replay; close early exits, preferably with 'with'.
    Closing early does not flush or certify an incomplete packet tail.
    """
    return TrackedReplay(path, ports=ports, label="Raw", make_tracker=RawTracker,
                         max_pending=MAX_PENDING_MESSAGES)
