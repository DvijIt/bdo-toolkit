"""Public single-use live raw-message capture."""
from pathlib import Path

from .._capture_options import LiveCaptureOptions
from .._tracked_capture import TrackedLiveSession
from ._collector import RawTracker
from .models import RawMessage

# Raw traffic is far denser than item or XP events: logins and zone loads
# arrive as thousands of messages per second.
DEFAULT_RAW_EVENT_QUEUE_SIZE = 16384


class LiveRawSession(TrackedLiveSession[RawMessage, None]):
    """Every server-to-client message from a passive live capture.

    No opcode profile, decoding or decryption. TCP gaps are counted in
    ``health`` and visible in message sequence numbers; native packet drops
    and queue overflows stop the session with ``CaptureIntegrityError``.
    Without ``live_options`` the event queue holds
    ``DEFAULT_RAW_EVENT_QUEUE_SIZE`` messages.
    """

    def __init__(self, *, live_options: LiveCaptureOptions | None = None,
                 capture_seconds: float | None = None,
                 save_pcap: str | Path | None = None) -> None:
        if live_options is not None and not isinstance(live_options, LiveCaptureOptions):
            raise TypeError("live_options must be LiveCaptureOptions or None")
        super().__init__(label="Raw", make_tracker=RawTracker,
                         live_options=live_options or LiveCaptureOptions(event_queue_size=DEFAULT_RAW_EVENT_QUEUE_SIZE),
                         capture_seconds=capture_seconds, save_pcap=save_pcap)
