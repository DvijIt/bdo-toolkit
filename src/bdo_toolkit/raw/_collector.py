"""Raw messages through the shared tracked-capture plumbing, opcode-free."""
from collections.abc import Callable, Iterable
from typing import NoReturn

from .._protocol import BDOFrame, FlowKey
from .._tracked_capture import TrackedFlowManager
from ..capture import CaptureIntegrityError
from ..events import Flow
from .models import RawMessage


def raw_message(frame: BDOFrame) -> RawMessage:
    flow = frame.context.flow
    return RawMessage(frame.message, frame.context.timestamp,
                      Flow(flow.source_ip, flow.source_port, flow.destination_ip, flow.destination_port),
                      frame.context.flow_generation, frame.index, frame.stream_sequence)


class RawTracker:
    """Publishes every framed message; raw keeps no status.

    TCP gaps and flow evictions are counted by the flow manager, not raised:
    the scanner resyncs after a gap and per-message sequence numbers show
    where bytes are missing. Only an exceeded delivery bound fails.
    """

    def __init__(self, on_message: Callable[[RawMessage], None]) -> None:
        self._publish = on_message

    def observe(self, frame: BDOFrame) -> None:
        self._publish(raw_message(frame))

    def close_flow(self, flow: FlowKey) -> None:
        del flow

    def snapshot(self) -> None:
        return None

    def failed_snapshot(self, reason: str) -> None:
        del reason

    def invalidate(self, reason: str) -> NoReturn:
        raise CaptureIntegrityError(reason)


def raw_flow_manager(on_message: Callable[[RawMessage], None], ports: Iterable[int], *,
                     live: bool = False) -> TrackedFlowManager[None]:
    """A flow manager that hands every framed message to ``on_message``."""
    return TrackedFlowManager(RawTracker(on_message), ports, live=live)
