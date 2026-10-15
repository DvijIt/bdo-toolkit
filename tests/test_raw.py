"""Profile-free raw server-to-client message replay and live capture."""
import pytest

import bdo_toolkit
from bdo_toolkit._protocol import BDOFrame, FlowKey, PacketContext
from bdo_toolkit.raw import DEFAULT_RAW_EVENT_QUEUE_SIZE, LiveRawSession, replay_raw
from bdo_toolkit import _tracked_capture as module
from bdo_toolkit import CaptureIntegrityError, LiveCaptureOptions
from bdo_toolkit._capture_runtime import CaptureStats
from test_agris_session import FakeCapture, wait_stopped

SERVER, CLIENT = ("192.0.2.1", 8889), ("192.0.2.2", 40000)
ISN = 5000


def message(opcode, payload=b"", flag=0):
    return (5 + len(payload)).to_bytes(2, "little") + bytes([flag]) + opcode.to_bytes(2, "little") + payload


MESSAGES = [message(0x1111, b"\x01\x02\x03"), message(0x2222, b"\xaa" * 40, flag=1), message(0x3333)]


def write_capture(path, chunks, *, syn=True, server=SERVER, start=ISN + 1, padding=0):
    from scapy.all import Ether, IP, TCP, Padding, Raw, wrpcap

    def packet(seq, flags, data=b""):
        p = Ether() / IP(src=server[0], dst=CLIENT[0]) / TCP(sport=server[1], dport=CLIENT[1], seq=seq, flags=flags)
        if data:
            p = p / Raw(data)
        if padding:
            p = p / Padding(b"\xee" * padding)
        return p

    output = [packet(ISN, "SA")] if syn else []
    seq = start
    for chunk in chunks:
        output.append(packet(seq, "PA", chunk))
        seq += len(chunk)
    for at, p in enumerate(output):
        p.time = 1000.0 + at
    wrpcap(str(path), output)


def test_replay_frames_every_message_without_profile(tmp_path):
    stream = b"".join(MESSAGES)
    path = tmp_path / "capture.pcap"
    write_capture(path, [stream[:4], stream[4:20], stream[20:]])  # Splits cut headers and payloads.
    with replay_raw(path) as replay:
        rows = list(replay)
        health = replay.health
    assert [r.message for r in rows] == MESSAGES
    assert [(r.opcode, r.flag, r.length) for r in rows] == [(0x1111, 0, 8), (0x2222, 1, 45), (0x3333, 0, 5)]
    assert [r.index for r in rows] == [0, 1, 2]
    assert [r.stream_sequence for r in rows] == [ISN + 1, ISN + 9, ISN + 54]
    assert {r.connection_epoch for r in rows} == {1}
    assert str(rows[0].flow) == "192.0.2.1:8889 -> 192.0.2.2:40000"
    assert health.messages_observed == 3 and health.capture_is_clean


def test_to_dict_is_json_ready(tmp_path):
    path = tmp_path / "capture.pcap"
    write_capture(path, [MESSAGES[0]])
    with replay_raw(path) as replay:
        row = next(replay).to_dict()
    assert row == {"schema_version": 1, "event_type": "raw_message", "observed_at": 1001.0,
                   "flow": {"source_ip": "192.0.2.1", "source_port": 8889,
                            "destination_ip": "192.0.2.2", "destination_port": 40000},
                   "connection_epoch": 1, "index": 0, "stream_sequence": ISN + 1, "length": 8,
                   "flag": 0, "opcode": "0x1111", "payload": "010203"}


def test_ethernet_padding_is_not_stream_payload(tmp_path):
    # Short frames are padded to the Ethernet minimum; reading the TCP payload
    # without the IP length would splice padding into the stream.
    path = tmp_path / "capture.pcap"
    write_capture(path, [message(0x0101), message(0x0202)], padding=6)
    with replay_raw(path) as replay:
        assert [r.message for r in replay] == [message(0x0101), message(0x0202)]


def test_midstream_attach_skips_the_partial_first_message(tmp_path):
    path = tmp_path / "capture.pcap"
    write_capture(path, [b"\xff" * 7 + b"".join(MESSAGES)], syn=False)
    with replay_raw(path) as replay:
        assert [r.message for r in replay] == MESSAGES


def test_only_requested_server_ports(tmp_path):
    path = tmp_path / "capture.pcap"
    write_capture(path, MESSAGES, server=("192.0.2.9", 9999))
    with replay_raw(path) as replay:
        assert list(replay) == []
    with replay_raw(path, ports=(9999,)) as replay:
        assert [r.flow.source_port for r in replay] == [9999] * 3


def test_close_early_ends_iteration(tmp_path):
    path = tmp_path / "capture.pcap"
    write_capture(path, MESSAGES)
    replay = replay_raw(path)
    assert next(replay).opcode == 0x1111
    replay.close()
    with pytest.raises(StopIteration):
        next(replay)


def test_missing_file_is_reported(tmp_path):
    with pytest.raises(FileNotFoundError):
        list(replay_raw(tmp_path / "absent.pcap"))


def live_frame(raw, index, at):
    flow = FlowKey(SERVER[0], SERVER[1], CLIENT[0], CLIENT[1])
    return BDOFrame(index, raw, PacketContext(at, flow, flow_generation=1), 1000 + index)


@pytest.fixture
def fake(monkeypatch):
    class Capture(FakeCapture):
        def start(self):
            self.running = True
            for index, raw in enumerate(MESSAGES):
                try:
                    self.callback(live_frame(raw, index, 1.0 + index))
                except BaseException as exc:
                    self.error = exc
                    break
    monkeypatch.setattr(module, "LivePacketCapture", lambda **kwargs: Capture(**kwargs))
    monkeypatch.setattr(module, "make_packet_handler", lambda manager: manager.observe)


def test_live_session_streams_messages(fake):
    owner = LiveRawSession(capture_seconds=.02)
    with owner:
        wait_stopped(owner)
        rows = list(owner.events())
    assert [r.message for r in rows] == MESSAGES
    assert owner.health.messages_observed == 3
    assert owner.health.packets_processed == 3
    assert owner.stop_reason == "duration"
    with pytest.raises(RuntimeError, match="single-use"):
        owner.start()


def test_live_session_default_queue_fits_raw_traffic():
    assert LiveRawSession()._options.event_queue_size == DEFAULT_RAW_EVENT_QUEUE_SIZE
    with pytest.raises(TypeError):
        LiveRawSession(live_options=object())  # type: ignore[arg-type]


def test_raw_is_a_public_subpackage():
    assert "raw" in bdo_toolkit.__all__
    assert bdo_toolkit.raw.replay_raw is replay_raw


def write_tcp_capture(path, segments, *, pcapng=False):
    """Explicit wire sequences allow gaps, retransmits and reused connections."""
    from scapy.all import Ether, IP, TCP, Raw, PcapWriter, PcapNgWriter

    writer_type = PcapNgWriter if pcapng else PcapWriter
    with writer_type(str(path)) as writer:
        for index, (seq, flags, data) in enumerate(segments):
            packet = (Ether() / IP(src=SERVER[0], dst=CLIENT[0])
                      / TCP(sport=SERVER[1], dport=CLIENT[1], seq=seq % (1 << 32), flags=flags))
            if data:
                packet /= Raw(data)
            packet.time = 1000.0 + index * .01
            writer.write(packet)


@pytest.mark.parametrize("pcapng", [False, True], ids=["pcap", "pcapng"])
@pytest.mark.parametrize("start", [ISN + 1, (1 << 32) - 3], ids=["ordinary", "wrap"])
def test_replay_reorders_and_deduplicates_with_unwrapped_sequences(tmp_path, pcapng, start):
    stream = b"".join(MESSAGES)
    path = tmp_path / ("capture.pcapng" if pcapng else "capture.pcap")
    write_tcp_capture(path, [
        (start - 1, "SA", b""),
        (start + 4, "PA", stream[4:]),  # Tail arrives before the split header.
        (start, "PA", stream[:4]),
        (start, "PA", stream),  # Full retransmission must not publish duplicates.
    ], pcapng=pcapng)
    with replay_raw(path) as replay:
        rows = list(replay)
    assert [r.message for r in rows] == MESSAGES
    assert [r.stream_sequence for r in rows] == [start, start + 8, start + 53]
    assert [r.index for r in rows] == [0, 1, 2]
    assert len({r.connection_epoch for r in rows}) == 1
    assert replay.health.messages_observed == 3
    assert replay.health.capture_is_clean


@pytest.mark.parametrize("ending", ["FA", "R"])
def test_replay_reconnect_retires_partial_frame_and_restarts_identity(tmp_path, ending):
    path = tmp_path / "capture.pcap"
    prefix = MESSAGES[0] + MESSAGES[1][:4]
    write_tcp_capture(path, [
        (ISN, "SA", b""),
        (ISN + 1, "PA", MESSAGES[0]),  # Prove a standalone frame boundary.
        (ISN + 1 + len(MESSAGES[0]), "PA", MESSAGES[1][:4]),
        (ISN + 1 + len(prefix), ending, b""),
        (ISN, "SA", b""),  # Same tuple and sequence numbers, new connection.
        (ISN + 1, "PA", b"".join(MESSAGES)),
    ])
    with replay_raw(path) as replay:
        rows = list(replay)
    assert [r.message for r in rows] == [MESSAGES[0], *MESSAGES]
    assert [r.index for r in rows] == [0, 0, 1, 2]
    assert rows[0].connection_epoch != rows[1].connection_epoch
    assert len({r.connection_epoch for r in rows[1:]}) == 1
    assert rows[0].flow == rows[1].flow
    assert replay.health.capture_is_clean


def test_replay_gap_at_eof_remains_visible_without_raising(tmp_path):
    path = tmp_path / "capture.pcap"
    resume = ISN + 1 + len(MESSAGES[0]) + 100
    write_tcp_capture(path, [
        (ISN, "SA", b""),
        (ISN + 1, "PA", MESSAGES[0]),
        (resume, "PA", b"".join(MESSAGES[1:])),
    ])
    with replay_raw(path) as replay:
        rows = list(replay)
    assert [r.message for r in rows] == MESSAGES
    assert rows[1].stream_sequence == resume
    assert len({r.connection_epoch for r in rows}) == 1
    assert [r.index for r in rows] == [0, 1, 2]
    assert replay.error is None
    assert replay.health.messages_observed == 3
    assert replay.health.tcp_gap_resets == 1
    assert not replay.health.capture_is_clean


def test_replay_overflow_discards_pending_messages_and_closes_reader(tmp_path, monkeypatch):
    from bdo_toolkit.raw import replay as raw_replay

    path = tmp_path / "capture.pcap"
    write_capture(path, [b"".join(MESSAGES)])
    monkeypatch.setattr(raw_replay, "MAX_PENDING_MESSAGES", 2)
    replay = replay_raw(path)
    with pytest.raises(CaptureIntegrityError, match="replay event bound") as caught:
        next(replay)
    assert replay.error is caught.value
    assert list(replay) == []
    replay.close()  # Idempotent after failure; reader no longer owns the file.
    path.unlink()


@pytest.mark.parametrize("queue", ["packet", "event"])
def test_live_queue_overflow_fails_closed_and_cleans_up(fake, queue):
    owner = LiveRawSession(live_options=LiveCaptureOptions(**{f"{queue}_queue_size": 1}))
    try:
        owner.start()
    except CaptureIntegrityError:
        pass  # Worker failure may race with startup readiness.
    wait_stopped(owner)
    with pytest.raises(CaptureIntegrityError, match="queue overflow") as caught:
        owner.poll(0)
    assert owner.error is caught.value
    assert getattr(owner.health, f"{queue}_queue_overflows") == 1
    assert not owner.health.capture_is_clean
    assert owner.stop_reason == "error"
    with pytest.raises(CaptureIntegrityError):
        owner.stop()
    assert owner.stopped and not owner.cleanup_incomplete


@pytest.mark.parametrize("counter", ["dropped", "interface_dropped"])
def test_live_native_loss_reported_at_shutdown_fails_closed(fake, monkeypatch, counter):
    # A backend can report loss only in its final statistics. Buffered events
    # must not escape as a successful capture after that final check.
    original_stop = FakeCapture.stop

    def stop_with_loss(capture):
        capture.stats = CaptureStats(**{counter: 1})
        return original_stop(capture)

    monkeypatch.setattr(FakeCapture, "stop", stop_with_loss)
    owner = LiveRawSession()
    owner.start()
    with pytest.raises(CaptureIntegrityError, match="dropped packets"):
        owner.stop()
    with pytest.raises(CaptureIntegrityError, match="dropped packets"):
        list(owner.events())
    assert getattr(owner.health, f"pcap_{counter}") == 1
    assert not owner.health.capture_is_clean
    assert owner.stopped and not owner.cleanup_incomplete
