"""Shared flow, replay and live-session plumbing for per-message trackers (raw, position, resource)."""
from collections import deque
from collections.abc import Callable, Generator, Iterable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from types import TracebackType
from typing import NoReturn, Protocol, Self, cast

from ._capture_backend import iter_pcap_file, make_packet_handler, open_packet_writer, validate_server_ports
from ._capture_options import LiveCaptureOptions
from ._capture_runtime import LivePacketCapture
from ._framing import FrameCollectorScanner
from ._observation_session import ObservationSession
from ._protocol import BDOFrame, FlowKey
from ._reassembly import FlowManager

# Readings one packet may release at once, e.g. when a held-back gap fills.
MAX_PENDING_READINGS = 4096


class FrameTracker[S](Protocol):
    def observe(self, frame: BDOFrame) -> None: ...

    def close_flow(self, flow: FlowKey) -> None: ...

    def snapshot(self) -> S: ...

    def failed_snapshot(self, reason: str) -> S: ...

    def invalidate(self, reason: str) -> NoReturn: ...


@dataclass(frozen=True)
class CaptureHealth:
    packets_processed: int = 0
    packets_accepted: int = 0
    messages_observed: int = 0
    packet_queue_overflows: int = 0
    event_queue_overflows: int = 0
    tcp_gap_resets: int = 0
    flow_state_evictions: int = 0
    pcap_received: int | None = None
    pcap_dropped: int | None = None
    pcap_interface_dropped: int | None = None
    capture_buffer_bytes: int | None = None
    cleanup_incomplete: bool = False

    @property
    def capture_is_clean(self) -> bool:
        """No known loss/cleanup failure; None native counters remain unknown."""
        return not (self.packet_queue_overflows or self.event_queue_overflows or self.tcp_gap_resets
                    or self.flow_state_evictions or self.pcap_dropped or self.pcap_interface_dropped
                    or self.cleanup_incomplete)

    def to_dict(self) -> dict[str, object]:
        return {**asdict(self), "capture_is_clean": self.capture_is_clean}


class TrackedFlowManager[S](FlowManager):
    """Frames every server message into one tracker and counts them; gaps only miss readings."""

    def __init__(self, tracker: FrameTracker[S], ports: Iterable[int], *, live: bool = False) -> None:
        self.tracker = tracker
        self.finishing = False
        self.messages = 0
        self.evictions = 0
        super().__init__(server_ports=validate_server_ports(ports),
                         scanner_factory=lambda: FrameCollectorScanner(self.observe),
                         track_flow_generations=True, max_flows=64, max_pending_segments=2048,
                         max_pending_bytes=8 * 1024 * 1024, defer_gap_timeouts=live,
                         on_flow_close=self._closed, on_flow_eviction=self._evicted)

    def observe(self, frame: BDOFrame) -> None:
        self.messages += 1
        self.tracker.observe(frame)

    def _closed(self, flow: FlowKey) -> None:
        # End of replay or capture is no logout.
        if not self.finishing:
            self.tracker.close_flow(flow)

    def _evicted(self) -> None:
        self.evictions += 1

    def finish(self) -> None:
        self.finishing = True
        try:
            super().finish()
        finally:
            self.finishing = False


class TrackedReplay[R, S](Iterator[R]):
    """Bounded single-consumer replay. Close early exits, preferably with 'with'.

    EOF retains the last observed state. On failure status becomes invalid
    and iteration raises. Closing early does not flush an incomplete packet tail.
    """

    def __init__(self, path: str | Path, *, ports: Iterable[int], label: str,
                 make_tracker: Callable[[Callable[[R], None]], FrameTracker[S]],
                 max_pending: int = MAX_PENDING_READINGS) -> None:
        self._label = label
        self._max_pending = max_pending
        self._pending: deque[R] = deque()
        self._tracker = make_tracker(self._enqueue)
        self._manager = TrackedFlowManager(self._tracker, ports)
        self._packets = cast(Generator[None, None, None], iter_pcap_file(Path(path), self._manager))
        self._processed = 0
        self._closed = self._finished = False
        self._error: BaseException | None = None

    def _enqueue(self, reading: R) -> None:
        if len(self._pending) >= self._max_pending:
            self._tracker.invalidate(f"{self._label} replay event bound exceeded")
        self._pending.append(reading)

    @property
    def error(self) -> BaseException | None:
        return self._error

    @property
    def status(self) -> S:
        return self._tracker.failed_snapshot(str(self._error)) if self._error else self._tracker.snapshot()

    @property
    def health(self) -> CaptureHealth:
        return CaptureHealth(packets_processed=self._processed, packets_accepted=self._processed,
                             messages_observed=self._manager.messages,
                             tcp_gap_resets=self._manager.tcp_gap_resets,
                             flow_state_evictions=self._manager.evictions)

    def __iter__(self) -> Self:
        return self

    def __next__(self) -> R:
        if self._closed:
            raise StopIteration
        try:
            while not self._pending:
                if self._finished:
                    self.close()
                    raise StopIteration
                try:
                    next(self._packets)
                except StopIteration:
                    self._finished = True
                else:
                    self._processed += 1
            return self._pending.popleft()
        except StopIteration:
            raise
        except BaseException as exc:
            self._error = exc
            try:
                self.close()
            except BaseException as cleanup:
                exc.add_note(f"{self._label} replay cleanup also failed: {cleanup!r}")
            raise

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._pending.clear()
            try:
                self._packets.close()
            except BaseException as exc:
                self._error = self._error or exc
                raise

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc_value: BaseException | None,
                 traceback: TracebackType | None) -> None:
        try:
            self.close()
        except BaseException as exc:
            if exc_value is None:
                raise
            exc_value.add_note(f"{self._label} replay cleanup also failed: {exc!r}")


class TrackedLiveSession[R, S](ObservationSession[R, S]):
    """Single-use live capture feeding one tracker; loss fails closed."""

    def __init__(self, *, label: str, make_tracker: Callable[[Callable[[R], None]], FrameTracker[S]],
                 live_options: LiveCaptureOptions | None, capture_seconds: float | None,
                 save_pcap: str | Path | None) -> None:
        if live_options is not None and not isinstance(live_options, LiveCaptureOptions):
            raise TypeError("live_options must be LiveCaptureOptions or None")
        options = live_options or LiveCaptureOptions()
        self._tracker = make_tracker(self._publish)
        self._tracked_manager: TrackedFlowManager[S] = TrackedFlowManager(self._tracker, options.ports, live=True)
        super().__init__(label=label, snapshot=self._tracker.snapshot, manager=self._tracked_manager,
                         refresh=lambda now: None, live_options=options, capture_seconds=capture_seconds,
                         save_pcap=save_pcap, capture_factory=lambda **kw: LivePacketCapture(**kw),
                         handler_factory=lambda manager: make_packet_handler(manager),
                         writer_factory=lambda path: open_packet_writer(path))

    @property
    def status(self) -> S:
        with self._state_lock:
            status, error = self._status, self._error
        return self._tracker.failed_snapshot(str(error)) if error else status

    @property
    def health(self) -> CaptureHealth:
        with self._state_lock:
            return CaptureHealth(
                packets_processed=self._processed, packets_accepted=self._accepted,
                messages_observed=self._tracked_manager.messages,
                packet_queue_overflows=self._packet_overflows, event_queue_overflows=self._event_overflows,
                tcp_gap_resets=self._manager.tcp_gap_resets, flow_state_evictions=self._tracked_manager.evictions,
                pcap_received=self._stats.received, pcap_dropped=self._stats.dropped,
                pcap_interface_dropped=self._stats.interface_dropped,
                capture_buffer_bytes=self._stats.capture_buffer_bytes,
                cleanup_incomplete=self._cleanup_incomplete)
