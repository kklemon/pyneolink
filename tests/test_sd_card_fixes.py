"""Regression tests for SD-card download/list/preview fixes."""

import threading

import pytest

import pyneolink.sd_card as sd_module
from pyneolink.core.bc import Header, InvalidMagicError, Message, ProtocolError, xml_document
from pyneolink.core.const import MSG, MSG_CLASS
from pyneolink.sd_card import (
    DownloadSizeMismatch,
    SdCard,
    _EmbeddedMp4SizeTracker,
    _FileInfoQuery,
    _download_raw,
    _embedded_mp4_total_size,
    _handle_queries,
    _is_download_continuation,
    _is_download_message,
    _preview_dump_query,
)
from datetime import datetime


class _BaseFakeCamera:
    config = type("Config", (), {"channel_id": 0})()
    sock = None

    def __init__(self):
        self.binary_msg_nums = set()
        self.sent = []
        self.recv_calls = 0
        self.next_msg_num = 0

    def send(self, msg_id, payload=b"", **kwargs):
        self.sent.append((msg_id, payload, kwargs))
        if kwargs.get("msg_num") is not None:
            return kwargs["msg_num"]
        if msg_id == MSG.UDP_KEEPALIVE:
            return 0
        self.next_msg_num += 1
        return self.next_msg_num

    def _recv(self, timeout=None):
        self.recv_calls += 1
        raise TimeoutError("no data")

    def close(self):
        pass

    def connect(self):
        pass

    def login(self):
        pass


# ---------------------------------------------------------------------------
# FIX 1: bounded number of full download passes
# ---------------------------------------------------------------------------


def test_download_bounds_total_passes_on_persistent_mismatch(tmp_path, monkeypatch):
    class FakeCamera:
        config = type("Config", (), {"channel_id": 0})()

        def __init__(self):
            self.reconnects = 0

        def reconnect(self):
            self.reconnects += 1

    camera = FakeCamera()
    sd_card = SdCard(camera)
    calls = []

    def fake_download_once(*args, **kwargs):
        calls.append(args)
        raise DownloadSizeMismatch("Downloaded 1 bytes, expected 5 bytes")

    monkeypatch.setattr(sd_card, "_download_once", fake_download_once)
    monkeypatch.setattr("pyneolink.sd_card.monotonic_clock.sleep", lambda seconds: None)

    with pytest.raises(DownloadSizeMismatch):
        sd_card.file({"file_name": "clip.mp4", "size": 5}).download(tmp_path)

    assert len(calls) == 4
    assert camera.reconnects == 3


def test_download_max_passes_kwarg_limits_attempts(tmp_path, monkeypatch):
    class FakeCamera:
        config = type("Config", (), {"channel_id": 0})()

        def __init__(self):
            self.reconnects = 0

        def reconnect(self):
            self.reconnects += 1

    camera = FakeCamera()
    sd_card = SdCard(camera)
    calls = []

    def fake_download_once(*args, **kwargs):
        calls.append(args)
        raise TimeoutError("stalled")

    monkeypatch.setattr(sd_card, "_download_once", fake_download_once)
    monkeypatch.setattr("pyneolink.sd_card.monotonic_clock.sleep", lambda seconds: None)

    with pytest.raises(TimeoutError):
        sd_card.file({"file_name": "clip.mp4", "size": 5}).download(tmp_path, max_passes=2)

    assert len(calls) == 2
    assert camera.reconnects == 1


def test_download_still_succeeds_on_a_later_pass(tmp_path, monkeypatch):
    class FakeCamera:
        config = type("Config", (), {"channel_id": 0})()

        def __init__(self):
            self.reconnects = 0

        def reconnect(self):
            self.reconnects += 1

    camera = FakeCamera()
    sd_card = SdCard(camera)
    calls = []

    def fake_download_once(*args, **kwargs):
        calls.append(args)
        if len(calls) < 3:
            raise DownloadSizeMismatch("Downloaded 1 bytes, expected 5 bytes")
        target = args[3]
        target.write_bytes(b"12345")
        return target

    monkeypatch.setattr(sd_card, "_download_once", fake_download_once)
    monkeypatch.setattr("pyneolink.sd_card.monotonic_clock.sleep", lambda seconds: None)

    result = sd_card.file({"file_name": "clip.mp4", "size": 5}).download(tmp_path)

    assert result == tmp_path / "clip.mp4"
    assert len(calls) == 3
    assert camera.reconnects == 2


# ---------------------------------------------------------------------------
# FIX 2: playback stop channel, drain, abandoned-strategy tracking
# ---------------------------------------------------------------------------


def test_stop_playback_download_uses_stored_channel_id():
    camera = _BaseFakeCamera()
    sd_card = SdCard(camera)
    sd_card._playback_channel_id = 42

    sd_card._stop_playback_download()

    msg_id, _payload, kwargs = camera.sent[-1]
    assert msg_id == MSG.FILE_PLAYBACK_STOP
    assert kwargs["channel_id"] == 42
    assert kwargs["msg_num"] == 0


def test_abandon_playback_strategy_sends_stop_drains_and_tracks_msg_nums():
    camera = _BaseFakeCamera()
    sd_card = SdCard(camera)
    sd_card._playback_channel_id = 33
    sd_card._last_attempt_msg_nums = {5}
    query = _FileInfoQuery("playback143/range-mainStream/bcmedia", MSG.FILE_PLAYBACK, b"", msg_class=MSG_CLASS.MODERN, channel_id=33, msg_num=0)

    sd_card._abandon_download_strategy(query, {}, recv_timeout=0.05)

    stops = [(msg_id, kwargs) for msg_id, _payload, kwargs in camera.sent if msg_id == MSG.FILE_PLAYBACK_STOP]
    assert stops == [(MSG.FILE_PLAYBACK_STOP, {"channel_id": 33, "msg_num": 0})]
    assert camera.recv_calls >= 1
    assert 5 in sd_card._abandoned_msg_nums


def test_download_once_stops_abandoned_playback_on_playback_channel(tmp_path, monkeypatch):
    camera = _BaseFakeCamera()
    sd_card = SdCard(camera)
    drains = []
    monkeypatch.setattr(sd_card, "_drain_camera_messages", lambda **kwargs: drains.append(kwargs))
    item = {
        "file_name": "clip.bin",
        "size": 8,
        "start_time": "2026-06-01T10:00:00",
        "end_time": "2026-06-01T10:00:30",
    }
    raw = _download_raw(item)

    with pytest.raises(ProtocolError):
        sd_card._download_once(
            item,
            dict(raw),
            "clip.bin",
            tmp_path / "clip.bin",
            expected_size=8,
            chunk_limit=0,
            progress=False,
            max_attempts=3,
            recv_timeout=0.05,
        )

    stops = [kwargs for msg_id, _payload, kwargs in camera.sent if msg_id == MSG.FILE_PLAYBACK_STOP]
    assert len(stops) == 2
    assert all(kwargs["channel_id"] == sd_card._playback_channel_id for kwargs in stops)
    assert 16 <= sd_card._playback_channel_id <= 63
    assert len(drains) == 2


def test_download_continuation_rejects_abandoned_msg_nums():
    msg = Message(Header(MSG.FILE_DOWNLOAD_VIDEO, 4, 0, 0, 5, 200, MSG_CLASS.FILE_DOWNLOAD), payload=b"data")

    assert _is_download_continuation(msg, MSG.FILE_DOWNLOAD_VIDEO, True)
    assert not _is_download_continuation(msg, MSG.FILE_DOWNLOAD_VIDEO, True, abandoned_msg_nums={5})
    assert _is_download_message(msg, MSG.FILE_DOWNLOAD_VIDEO, {7}, True)
    assert not _is_download_message(msg, MSG.FILE_DOWNLOAD_VIDEO, {7}, True, abandoned_msg_nums={5})


# ---------------------------------------------------------------------------
# FIX 3: startup deadline honors cumulative waiting
# ---------------------------------------------------------------------------


def test_download_query_tolerates_multiple_startup_timeouts(tmp_path):
    camera = _BaseFakeCamera()
    sd_card = SdCard(camera)
    query = _FileInfoQuery("download13/id/class6482", MSG.FILE_DOWNLOAD, b"", msg_class=MSG_CLASS.FILE_DOWNLOAD)

    with pytest.raises(TimeoutError):
        sd_card._download_with_query(
            query,
            tmp_path / "clip.part",
            expected_size=8,
            chunk_limit=0,
            idle_timeouts=10,
            progress=False,
            recv_timeout=0.2,
        )

    assert 2 <= camera.recv_calls <= 10


# ---------------------------------------------------------------------------
# FIX 4: file_type is forwarded to camera list queries
# ---------------------------------------------------------------------------


def _file_list_xml():
    return b"""<?xml version="1.0" encoding="UTF-8" ?>
<body>
<FileInfoList version="1.1">
<FileInfo><fileName>clip.mp4</fileName></FileInfo>
</FileInfoList>
</body>"""


def test_list_forwards_file_type_to_record_types():
    class FakeCamera:
        config = type("Config", (), {"channel_id": 0})()

        def __init__(self):
            self.payloads = []

        def command(self, msg_id, payload=b"", extension=b""):
            self.payloads.append(payload)
            if b"<DayRecords" in payload:
                xml = b'<?xml version="1.0" encoding="UTF-8" ?>\n<body><DayRecords version="1.1" /></body>'
            else:
                xml = _file_list_xml()
            return Message(Header(msg_id, len(xml), 0, 0, 1, 200, MSG_CLASS.MODERN), payload=xml)

    camera = FakeCamera()
    files = SdCard(camera).list(start="2026-06-01", end="2026-06-01", file_type="md")

    assert files[0]["file_name"] == "clip.mp4"
    assert any(b"<recordType>md</recordType>" in payload for payload in camera.payloads)

    camera.payloads.clear()
    SdCard(camera).list(start="2026-06-01", end="2026-06-01")
    assert any(
        b"<recordType>manual, sched, io, md, people, face, vehicle, dog_cat, visitor</recordType>" in payload
        for payload in camera.payloads
    )


def test_handle_queries_use_file_type_as_record_types():
    start = datetime(2026, 6, 1, 0, 0, 0)
    end = datetime(2026, 6, 1, 23, 59, 59)

    default_queries = _handle_queries(0, start, end, "mainStream", "All")
    typed_queries = _handle_queries(0, start, end, "mainStream", "people")

    assert all(b"<recordType>manual, sched, io, md, people, face, vehicle, dog_cat, visitor</recordType>" in query.payload for query in default_queries)
    assert all(b"<recordType>people</recordType>" in query.payload for query in typed_queries)
    assert not hasattr(sd_module, "_file_info_queries")


# ---------------------------------------------------------------------------
# FIX 5: binary_msg_nums registered by a download are discarded afterwards
# ---------------------------------------------------------------------------


def test_download_discards_registered_binary_msg_nums(tmp_path):
    class RecordingSet(set):
        def __init__(self):
            super().__init__()
            self.added = []

        def add(self, value):
            self.added.append(value)
            super().add(value)

    class FakeCamera(_BaseFakeCamera):
        def __init__(self):
            super().__init__()
            self.binary_msg_nums = RecordingSet()
            xml = xml_document('<Ignored version="1.1" />')
            self.replies = [
                Message(
                    Header(MSG.FILE_DOWNLOAD, len(xml), 0, 0, 7, 200, MSG_CLASS.MODERN),
                    extension=b"<Extension><binaryData>1</binaryData></Extension>",
                    payload=xml,
                ),
                Message(Header(MSG.FILE_DOWNLOAD, 8, 0, 0, 7, 200, MSG_CLASS.FILE_DOWNLOAD), payload=b"12345678"),
            ]

        def send(self, msg_id, payload=b"", **kwargs):
            super().send(msg_id, payload, **kwargs)
            if kwargs.get("msg_num") is not None:
                return kwargs["msg_num"]
            return 7

        def _recv(self, timeout=None):
            self.recv_calls += 1
            if not self.replies:
                raise TimeoutError("no more replies")
            return self.replies.pop(0)

    camera = FakeCamera()
    sd_card = SdCard(camera)
    query = _FileInfoQuery("download13/id/class6482", MSG.FILE_DOWNLOAD, b"", msg_class=MSG_CLASS.FILE_DOWNLOAD)

    written = sd_card._download_with_query(
        query,
        tmp_path / "clip.part",
        expected_size=8,
        chunk_limit=0,
        idle_timeouts=10,
        progress=False,
        recv_timeout=0.2,
    )

    assert written == 8
    assert 7 in camera.binary_msg_nums.added
    assert len(camera.binary_msg_nums) == 0


# ---------------------------------------------------------------------------
# FIX 9: pagination only stops on a repeated or empty page
# ---------------------------------------------------------------------------


def _paged_camera(pages):
    class FakeCamera:
        config = type("Config", (), {"channel_id": 0})()

        def __init__(self):
            self.detail_calls = 0

        def command(self, msg_id, payload=b"", extension=b""):
            if b"<DayRecords" in payload:
                xml = b'<?xml version="1.0" encoding="UTF-8" ?>\n<body><DayRecords version="1.1" /></body>'
            elif b"<handle>" not in payload:
                xml = (
                    b'<?xml version="1.0" encoding="UTF-8" ?>\n<body>\n<FileInfoList version="1.1">\n'
                    b"<FileInfo><channelId>0</channelId><handle>1</handle><fileName>inline.mp4</fileName></FileInfo>\n"
                    b"</FileInfoList>\n</body>"
                )
            else:
                page = pages[min(self.detail_calls, len(pages) - 1)]
                self.detail_calls += 1
                rows = "".join(f"<FileInfo><fileName>{name}</fileName></FileInfo>" for name in page)
                xml = (
                    '<?xml version="1.0" encoding="UTF-8" ?>\n<body>\n'
                    f'<FileInfoList version="1.1">{rows}</FileInfoList>\n</body>'
                ).encode("utf-8")
            return Message(Header(msg_id, len(xml), 0, 0, 1, 200, MSG_CLASS.MODERN), payload=xml)

    return FakeCamera()


def test_list_continues_when_first_page_only_repeats_inline_files():
    camera = _paged_camera(
        [
            ["inline.mp4"],
            ["fresh.mp4"],
            ["fresh.mp4"],
        ]
    )

    files = SdCard(camera).list(start="2026-06-02", end="2026-06-02")

    assert sorted(item["file_name"] for item in files) == ["fresh.mp4", "inline.mp4"]
    assert camera.detail_calls == 3


def test_list_stops_when_camera_repeats_the_same_page():
    camera = _paged_camera(
        [
            ["page1-a.mp4", "page1-b.mp4"],
            ["page1-a.mp4", "page1-b.mp4"],
        ]
    )

    files = SdCard(camera).list(start="2026-06-02", end="2026-06-02")

    assert [item["file_name"] for item in files] == ["inline.mp4", "page1-a.mp4", "page1-b.mp4"]
    assert camera.detail_calls == 2


# ---------------------------------------------------------------------------
# FIX 6: partial .part files are cleaned up
# ---------------------------------------------------------------------------


def test_failed_attempts_remove_non_empty_part_files(tmp_path, monkeypatch):
    camera = _BaseFakeCamera()
    sd_card = SdCard(camera)

    def fake_query(query, part_path, **kwargs):
        part_path.write_bytes(b"partial-data")
        raise TimeoutError("interrupted")

    monkeypatch.setattr(sd_card, "_download_with_query", fake_query)
    item = {"file_name": "clip.bin", "size": 8}
    raw = _download_raw(item)

    with pytest.raises(ProtocolError):
        sd_card._download_once(
            item,
            dict(raw),
            "clip.bin",
            tmp_path / "clip.bin",
            expected_size=8,
            chunk_limit=0,
            progress=False,
            max_attempts=2,
            recv_timeout=0.05,
        )

    assert list(tmp_path.glob("*.part")) == []


def test_successful_download_removes_stale_part_files(tmp_path, monkeypatch):
    camera = _BaseFakeCamera()
    sd_card = SdCard(camera)
    stale = tmp_path / "clip.bin.oldstrategy.part"
    stale.write_bytes(b"stale")

    def fake_query(query, part_path, **kwargs):
        part_path.write_bytes(b"12345678")
        return 8

    monkeypatch.setattr(sd_card, "_download_with_query", fake_query)
    item = {"file_name": "clip.bin", "size": 8}
    raw = _download_raw(item)

    result = sd_card._download_once(
        item,
        dict(raw),
        "clip.bin",
        tmp_path / "clip.bin",
        expected_size=8,
        chunk_limit=0,
        progress=False,
        max_attempts=1,
        recv_timeout=0.05,
    )

    assert result.read_bytes() == b"12345678"
    assert not stale.exists()
    assert list(tmp_path.glob("*.part")) == []


# ---------------------------------------------------------------------------
# FIX 7: day_records tolerates per-variant timeouts
# ---------------------------------------------------------------------------


def test_day_records_tries_next_variant_after_timeout():
    class FakeCamera:
        config = type("Config", (), {"channel_id": 0})()

        def __init__(self):
            self.calls = 0

        def command(self, msg_id, payload=b"", extension=b""):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("no answer")
            xml = xml_document('<DayRecords version="1.1"><channelId>0</channelId></DayRecords>')
            return Message(Header(msg_id, len(xml), 0, 0, 1, 200, MSG_CLASS.MODERN), payload=xml)

    camera = FakeCamera()
    sd_card = SdCard(camera)

    result = sd_card.day_records("2026-06-01")

    assert camera.calls == 2
    assert result
    assert sd_card.last_attempts[0].endswith("timeout")


# ---------------------------------------------------------------------------
# FIX 8: preview dump size cap and incremental MP4 size tracking
# ---------------------------------------------------------------------------


class _DumpCamera(_BaseFakeCamera):
    def _recv_matching(self, msg_id, msg_num):
        return Message(Header(msg_id, 40, 0, 0, msg_num, 200, MSG_CLASS.FILE_DOWNLOAD), payload=b"\x01" * 40)

    def _recv(self, timeout=None):
        self.recv_calls += 1
        return Message(Header(MSG.FILE_DOWNLOAD_VIDEO, 40, 0, 0, 5, 200, MSG_CLASS.FILE_DOWNLOAD), payload=b"\x01" * 40)

    def send(self, msg_id, payload=b"", **kwargs):
        super().send(msg_id, payload, **kwargs)
        return 5


def test_read_preview_dump_default_cap_raises_clear_error(monkeypatch):
    monkeypatch.setattr(sd_module, "_PREVIEW_DUMP_MAX_BYTES", 64)
    sd_card = SdCard(_DumpCamera())
    query = _preview_dump_query(0, "clip.mp4", {})

    with pytest.raises(ProtocolError) as info:
        sd_card._read_preview_dump(query, progress=False, recv_timeout=0.05, idle_timeouts=2, max_bytes=None)

    assert "safety limit" in str(info.value)


def test_read_preview_dump_explicit_max_bytes_truncates_without_error():
    sd_card = SdCard(_DumpCamera())
    query = _preview_dump_query(0, "clip.mp4", {})

    data = sd_card._read_preview_dump(query, progress=False, recv_timeout=0.05, idle_timeouts=2, max_bytes=64)

    assert len(data) == 64


def test_embedded_mp4_size_tracker_matches_full_scan():
    junk = b"1002" + b"\x00" * 28
    ftyp = b"\x00\x00\x00\x18ftypisof" + b"\x00\x00\x00\x01isofhvc1"
    moov = b"\x00\x00\x00\x10moov" + b"\x00" * 8
    mdat_header = b"\x00\x00\x00\x64mdat"
    tracker = _EmbeddedMp4SizeTracker()

    assert tracker.feed(junk) is None
    assert tracker.feed(junk + ftyp) is None
    assert tracker.feed(junk + ftyp + moov) is None
    full = junk + ftyp + moov + mdat_header
    expected = len(junk) + len(ftyp) + len(moov) + 100
    assert tracker.feed(full) == expected
    assert tracker.feed(full) == expected
    assert _embedded_mp4_total_size(full) == expected


def test_embedded_mp4_size_tracker_gives_up_without_ftyp():
    tracker = _EmbeddedMp4SizeTracker()
    blob = b"\x00" * (64 * 1024)

    assert tracker.feed(blob) is None
    assert tracker.feed(blob + b"\x00\x00\x00\x18ftypisof") is None


# ---------------------------------------------------------------------------
# FIX 10: remove() raises NotImplementedError immediately
# ---------------------------------------------------------------------------


def test_remove_raises_not_implemented_regardless_of_confirm():
    sd_card = SdCard(_BaseFakeCamera())

    with pytest.raises(NotImplementedError):
        sd_card.remove({"file_name": "clip.mp4"})
    with pytest.raises(NotImplementedError):
        sd_card.remove({"file_name": "clip.mp4"}, confirm=True)


# ---------------------------------------------------------------------------
# FIX 11: raw-tail recovery reconnects the shared transport
# ---------------------------------------------------------------------------


def test_raw_tail_recovery_forces_reconnect(tmp_path):
    class RawTailSock:
        def __init__(self):
            self.chunks = [b"5678"]

        def recv_some(self, limit):
            if self.chunks:
                return self.chunks.pop(0)
            raise TimeoutError("done")

    class FakeCamera(_BaseFakeCamera):
        def __init__(self):
            super().__init__()
            self.sock = RawTailSock()
            self.lifecycle = []

        def _recv(self, timeout=None):
            self.recv_calls += 1
            raise InvalidMagicError(0x11223344, b"1234")

        def close(self):
            self.lifecycle.append("close")

        def connect(self):
            self.lifecycle.append("connect")

        def login(self):
            self.lifecycle.append("login")

    camera = FakeCamera()
    sd_card = SdCard(camera)
    item = {"file_name": "clip.bin", "size": 8}
    raw = _download_raw(item)

    result = sd_card._download_once(
        item,
        dict(raw),
        "clip.bin",
        tmp_path / "clip.bin",
        expected_size=8,
        chunk_limit=0,
        progress=False,
        max_attempts=1,
        recv_timeout=0.1,
    )

    assert result.read_bytes() == b"12345678"
    assert camera.lifecycle == ["close", "connect", "login"]


# ---------------------------------------------------------------------------
# FIX 12: preview close/worker hygiene
# ---------------------------------------------------------------------------


def test_preview_close_keeps_cache_while_worker_is_alive(tmp_path):
    class FakeThread:
        def __init__(self, alive):
            self.alive = alive
            self.joins = []

        def is_alive(self):
            return self.alive

        def join(self, timeout=None):
            self.joins.append(timeout)

    preview = SdCard(_BaseFakeCamera()).file({"file_name": "clip.mp4"}).preview(cache=tmp_path)
    preview.path.write_bytes(b"cached")

    preview._thread = FakeThread(alive=True)
    with pytest.warns(RuntimeWarning):
        preview.close()
    assert preview.path.exists()
    assert len(preview._thread.joins) == 2

    preview._thread = FakeThread(alive=False)
    preview.close()
    assert not preview.path.exists()


def test_cache_preview_cancels_camera_transfer_on_stop(tmp_path):
    class FakeCamera(_BaseFakeCamera):
        def _recv_matching(self, msg_id, msg_num):
            return Message(Header(msg_id, 0, 0, 0, msg_num, 200, MSG_CLASS.FILE_DOWNLOAD), payload=b"")

        def send(self, msg_id, payload=b"", **kwargs):
            super().send(msg_id, payload, **kwargs)
            return 5

    camera = FakeCamera()
    sd_card = SdCard(camera)
    stop = threading.Event()
    stop.set()
    ready = threading.Event()

    sd_card._cache_preview(
        {"file_name": "clip.mp4"},
        tmp_path / "cache.mp4",
        stream_type="mainStream",
        channel_id=None,
        max_bytes=None,
        progress=False,
        recv_timeout=0.05,
        idle_timeouts=1,
        ready=ready,
        stop=stop,
    )

    assert any(msg_id == MSG.FILE_PLAYBACK_STOP for msg_id, _payload, _kwargs in camera.sent)
    assert ready.is_set()
