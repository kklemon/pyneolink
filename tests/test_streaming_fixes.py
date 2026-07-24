"""Regression tests for streaming/protocol-core fixes.

Covers: resumable receive buffer (TCP desync), UDP duplicate handling,
MPEG-TS timestamp wraparound, media parser hardening, snapshot path
sanitization, and keepalive reply gating.
"""
from __future__ import annotations

import socket as socket_mod

import pytest

from pyneolink.camera import Camera
from pyneolink.core.bc import InvalidMagicError, encode_modern, recv_message
from pyneolink.core.const import MSG, MSG_CLASS
from pyneolink.core.crypto import Cipher
from pyneolink.core.media import MediaPacket, MediaParser
from pyneolink.core.udp_transport import UdpBcConnection, decode_udp_packet, encode_udp_data
from pyneolink.internal.snapshot import snapshot_output_path
from pyneolink.stream_server import MpegTsMuxer


class ScriptedSocket:
    """Serves recv() from a list of chunks; an exception instance raises."""

    def __init__(self, script):
        self.script = list(script)
        self.sent: list[bytes] = []

    def settimeout(self, _timeout):
        pass

    def recv(self, size):
        if not self.script:
            return b""
        item = self.script[0]
        if isinstance(item, Exception):
            self.script.pop(0)
            raise item
        chunk = item[:size]
        remainder = item[size:]
        if remainder:
            self.script[0] = remainder
        else:
            self.script.pop(0)
        return chunk

    def sendall(self, data):
        self.sent.append(data)

    def close(self):
        pass


def test_recv_message_resumes_after_timeout_mid_message():
    message = encode_modern(MSG.VIDEO, 42, b"<body>hello</body>", cipher=Cipher("bc"))
    sock = ScriptedSocket([message[:10], socket_mod.timeout("stall"), message[10:]])
    buffer = bytearray()
    with pytest.raises(TimeoutError):
        recv_message(sock, Cipher("bc"), buffer=buffer)
    # Partial bytes must survive in the buffer so the retry stays in sync.
    assert len(buffer) == 10
    msg = recv_message(sock, Cipher("bc"), buffer=buffer)
    assert msg.header.msg_num == 42
    assert msg.payload == b"<body>hello</body>"
    assert not buffer


def test_recv_message_consumes_bad_header_and_recovers():
    garbage = b"\x00" * 20
    message = encode_modern(MSG.VIDEO, 7, b"<body>ok</body>", cipher=Cipher("bc"))
    sock = ScriptedSocket([garbage + message])
    buffer = bytearray()
    with pytest.raises(InvalidMagicError):
        recv_message(sock, Cipher("bc"), buffer=buffer)
    msg = recv_message(sock, Cipher("bc"), buffer=buffer)
    assert msg.header.msg_num == 7


class FakeUdpSocket:
    def __init__(self):
        self.incoming: list[bytes] = []
        self.sent: list[bytes] = []

    def settimeout(self, _timeout):
        pass

    def recvfrom(self, _size):
        if not self.incoming:
            raise socket_mod.timeout()
        return self.incoming.pop(0), ("1.2.3.4", 9000)

    def sendto(self, data, _addr):
        self.sent.append(data)

    def close(self):
        pass


def _udp_conn(limit=None):
    fake = FakeUdpSocket()
    conn = UdpBcConnection(fake, ("1.2.3.4", 9000), client_id=1, camera_id=2, timeout=0.2)
    conn.set_max_pending_chunks(limit)
    return fake, conn


def test_udp_duplicates_are_not_stored():
    fake, conn = _udp_conn()
    for pid in range(4):
        fake.incoming.append(encode_udp_data(1, pid, b"x" * 4))
        conn._recv_one()
    assert conn.recv(16) == b"x" * 16
    # Camera resends already-consumed packets (lost ack).
    for pid in range(4):
        fake.incoming.append(encode_udp_data(1, pid, b"x" * 4))
        conn._recv_one()
    assert conn.duplicate_packets_received == 4
    assert not conn.recv_chunks
    assert conn.next_recv_id == 4


def test_udp_duplicates_do_not_trigger_spurious_overflow():
    fake, conn = _udp_conn(limit=5)
    for pid in range(4):
        fake.incoming.append(encode_udp_data(1, pid, b"x" * 4))
        conn._recv_one()
    conn.recv(16)
    for pid in [0, 1, 2, 3, 4, 5]:  # 4 duplicates + 2 fresh
        fake.incoming.append(encode_udp_data(1, pid, b"x" * 4))
        conn._recv_one()
    # A small real gap must not overflow just because duplicates arrived.
    for pid in [10, 11]:
        fake.incoming.append(encode_udp_data(1, pid, b"x" * 4))
        conn._recv_one()
    assert len(conn.recv_chunks) == 2


def test_udp_settimeout_zero_and_none():
    _fake, conn = _udp_conn()
    conn.settimeout(None)
    assert conn.timeout == 0.2
    conn.settimeout(5.0)
    assert conn.timeout == 5.0


def test_decode_udp_packet_rejects_truncated_payload():
    packet = encode_udp_data(1, 0, b"payload")
    assert decode_udp_packet(packet) is not None
    assert decode_udp_packet(packet[:-3]) is None


def _frame(ts_us):
    return MediaPacket("pframe", "H264", ts_us, b"\x00\x00\x00\x01\x41")


def test_muxer_pts_survives_microsecond_wraparound():
    mux = MpegTsMuxer("H264", fps=15)
    list(mux.feed(_frame(2**32 - 50_000)))
    before = mux.video_pts
    list(mux.feed(_frame(16_666)))  # wrapped: ~66ms later
    after = mux.video_pts
    assert after > before
    assert (after - before) < 90_000  # less than a second of PTS advance


def test_muxer_pts_clamps_implausible_jumps():
    mux = MpegTsMuxer("H264", fps=10)
    list(mux.feed(_frame(1_000_000)))
    before = mux.video_pts
    list(mux.feed(_frame(500_000_000)))  # +499s: camera-side discontinuity
    assert mux.video_pts - before == 90_000 // 10


def test_media_parser_resyncs_on_oversized_frame():
    parser = MediaParser()
    bogus = b"00dc" + b"H264" + (2**31).to_bytes(4, "little") + b"\x00" * 12
    packets = list(parser.feed(bogus))
    assert packets == []
    # Parser must not sit waiting for 2 GiB; buffer shrinks via resync.
    assert len(parser._buf) < len(bogus)


def test_media_parser_resync_keeps_split_magic_prefix():
    parser = MediaParser()
    info = b"1001" + (32).to_bytes(4, "little") + (640).to_bytes(4, "little") + (360).to_bytes(4, "little") + bytes([0, 15]) + b"\x00" * 14
    assert len(info) == 32
    garbage = b"ZZZZZZZZZZ"
    packets = list(parser.feed(garbage + info[:2]))
    assert packets == []
    packets = list(parser.feed(info[2:]))
    assert [p.kind for p in packets] == ["info"]
    assert packets[0].fps == 15


def test_snapshot_output_path_sanitizes_camera_file_name(tmp_path):
    target = tmp_path / "snaps"
    target.mkdir()
    evil_relative = snapshot_output_path(target, "../../escape.jpg")
    assert evil_relative.parent == target
    assert evil_relative.name == "escape.jpg"
    evil_absolute = snapshot_output_path(target, "/etc/cron.d/x.jpg")
    assert evil_absolute.parent == target
    assert evil_absolute.name == "x.jpg"
    normal = snapshot_output_path(target, "front.jpg")
    assert normal == target / "front.jpg"


def _offline_camera():
    return Camera(address="127.0.0.1", state_path=None)


def test_recv_replies_only_to_keepalive_requests():
    cam = _offline_camera()
    request = encode_modern(MSG.UDP_KEEPALIVE, 5, cipher=Cipher("bc"))
    reply = encode_modern(MSG.UDP_KEEPALIVE, 5, response_code=405, cipher=Cipher("bc"))

    sock = ScriptedSocket([reply])
    cam.sock = sock
    cam._recv(timeout=1.0)
    assert sock.sent == []  # a reply from the camera must not be answered

    cam2 = _offline_camera()
    sock2 = ScriptedSocket([request])
    cam2.sock = sock2
    cam2._recv(timeout=1.0)
    assert len(sock2.sent) == 1  # a genuine request gets exactly one answer


def test_recv_rejects_non_positive_timeout_without_reading():
    cam = _offline_camera()
    cam.sock = object()  # would explode if any socket call happened
    with pytest.raises(TimeoutError):
        cam._recv(timeout=0.0)
    with pytest.raises(TimeoutError):
        cam._recv(timeout=-1.0)


def test_close_clears_receive_state():
    cam = _offline_camera()
    cam.sock = ScriptedSocket([])
    cam._rx_buffer.extend(b"partial")
    cam.binary_msg_nums.add(99)
    cam.close()
    assert not cam._rx_buffer
    assert not cam.binary_msg_nums
