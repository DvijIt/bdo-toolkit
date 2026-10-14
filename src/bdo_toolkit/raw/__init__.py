"""Raw server-to-client BDO messages for protocol research.

Every length-framed message, opcode-free: no opcode profile, decoding or
decryption. Framing waits for evidence of a message boundary, so a capture
that attaches mid-stream skips the bytes before the first one.
"""
from .models import RawCaptureHealth, RawMessage
from .replay import RawReplay, replay_raw
from .session import DEFAULT_RAW_EVENT_QUEUE_SIZE, LiveRawSession

__all__ = ["DEFAULT_RAW_EVENT_QUEUE_SIZE", "LiveRawSession", "RawCaptureHealth", "RawMessage",
           "RawReplay", "replay_raw"]
