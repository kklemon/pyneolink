from __future__ import annotations

import math
import struct
import subprocess
import sys
import threading
import time
import types
import wave
from pathlib import Path

import pytest

import pyneolink.internal.voice as internal_voice
from pyneolink.internal.voice import (
    ImaAdpcmEncoder,
    TalkConfig,
    adpcm_blocks_from_file,
    adpcm_blocks_from_microphone,
)
from pyneolink.voice import Voice

FFMPEG = "/usr/bin/ffmpeg"


# --- helpers -----------------------------------------------------------------


def talk_config(*, sample_rate: int = 16000, length_per_encoder: int = 1016) -> TalkConfig:
    return TalkConfig(
        channel_id=0,
        duplex="FDX",
        audio_stream_mode="followVideoStream",
        audio_type="adpcm",
        sample_rate=sample_rate,
        sample_precision=16,
        length_per_encoder=length_per_encoder,
        sound_track="mono",
    )


def sine_samples(sample_rate: int, seconds: float, *, frequency: float = 440.0, amplitude: int = 8000) -> list[int]:
    count = int(sample_rate * seconds)
    return [int(amplitude * math.sin(2.0 * math.pi * frequency * pos / sample_rate)) for pos in range(count)]


def write_wav(path: Path, pcm: bytes, sample_rate: int) -> None:
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(pcm)


def wrap_ima_wav(blocks: list[bytes], sample_rate: int, block_align: int, samples_per_block: int) -> bytes:
    data = b"".join(block for block in blocks if len(block) == block_align)
    total_samples = (len(data) // block_align) * samples_per_block
    byte_rate = (sample_rate * block_align) // samples_per_block
    fmt = struct.pack("<HHIIHHHH", 0x11, 1, sample_rate, byte_rate, block_align, 4, 2, samples_per_block)
    fact = struct.pack("<I", total_samples)
    chunks = b"WAVE"
    chunks += b"fmt " + struct.pack("<I", len(fmt)) + fmt
    chunks += b"fact" + struct.pack("<I", len(fact)) + fact
    chunks += b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", len(chunks)) + chunks


def ffmpeg_decode(ima_wav: bytes) -> list[int]:
    result = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "wav", "-i", "pipe:0", "-f", "s16le", "pipe:1"],
        input=ima_wav,
        capture_output=True,
        check=True,
    )
    out = result.stdout
    return list(struct.unpack("<" + "h" * (len(out) // 2), out[: len(out) - len(out) % 2]))


def correlation(a: list[int], b: list[int]) -> float:
    size = min(len(a), len(b))
    a, b = a[:size], b[:size]
    num = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if not norm_a or not norm_b:
        return 0.0
    return num / (norm_a * norm_b)


def swap_nibbles(block: bytes) -> bytes:
    return block[:4] + bytes(((byte << 4) & 0xF0) | (byte >> 4) for byte in block[4:])


@pytest.fixture(scope="module")
def sine_wav(tmp_path_factory) -> tuple[Path, list[int]]:
    path = tmp_path_factory.mktemp("voice-fixes") / "sine.wav"
    samples = sine_samples(16000, 1.0)
    write_wav(path, struct.pack("<" + "h" * len(samples), *samples), 16000)
    return path, samples


@pytest.fixture(scope="module")
def long_silence_wav(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("voice-fixes-long") / "silence.wav"
    write_wav(path, b"\x00" * (2 * 16000 * 30), 16000)
    return path


@pytest.fixture
def spy_popen(monkeypatch):
    created: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def _spy(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        created.append(process)
        return process

    monkeypatch.setattr(internal_voice.subprocess, "Popen", _spy)
    yield created
    for process in created:
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=5)
            except Exception:
                pass


class StubSock:
    def __init__(self) -> None:
        self.maintain_calls = 0

    def maintain(self) -> None:
        self.maintain_calls += 1


class StubCamera:
    debug = False

    def __init__(self, sock: object | None = None) -> None:
        self.sock = StubSock() if sock is None else sock
        self.config = types.SimpleNamespace(channel_id=0)
        self.sent: list[int] = []
        self.recv_calls = 0
        self.recv_matching_calls = 0
        self._msg = 100

    def _next_msg(self) -> int:
        self._msg += 1
        return self._msg

    def send(self, msg_id, payload=b"", *, extension=None, msg_num=None):  # noqa: ANN001
        self.sent.append(msg_id)
        return msg_num if msg_num is not None else self._next_msg()

    def _recv(self, timeout=None):  # noqa: ANN001
        self.recv_calls += 1
        raise TimeoutError

    def _recv_matching(self, msg_id, msg_num):  # noqa: ANN001
        self.recv_matching_calls += 1
        return types.SimpleNamespace(header=types.SimpleNamespace(response_code=200, msg_id=msg_id, msg_num=msg_num))


# --- FIX 1: ADPCM nibble order -------------------------------------------------


def test_encode_block_packs_first_sample_in_low_nibble():
    # Hand-computed IMA ADPCM: starting from predictor=0/index=0, encoding
    # sample 1000 gives code 0x7 (predictor -> 11, index -> 8) and encoding
    # sample 0 afterwards gives code 0xA. GStreamer's adpcmenc and ffmpeg's
    # adpcm_ima_wav pack the first code into the LOW nibble, so the single
    # data byte must be 0xA7, not 0x7A.
    block = ImaAdpcmEncoder().encode_block([0, 1000, 0])

    assert block == b"\x00\x00\x00\x00\xa7"
    assert block[4] & 0x0F == 0x7
    assert block[4] >> 4 == 0xA


def test_encode_block_pads_trailing_odd_nibble_into_low_nibble():
    # A lone trailing code must sit in the low nibble with a zero high nibble.
    block = ImaAdpcmEncoder().encode_block([0, 1000])

    assert block == b"\x00\x00\x00\x00\x07"


def test_python_codec_first_block_matches_ffmpeg_encoder(sine_wav):
    path, _ = sine_wav
    config = talk_config()

    python_blocks = list(adpcm_blocks_from_file(path, config, codec="python"))
    ffmpeg_blocks = list(adpcm_blocks_from_file(path, config, codec="ffmpeg"))

    assert python_blocks and ffmpeg_blocks
    assert len(python_blocks[0]) == config.block_align
    assert len(ffmpeg_blocks[0]) == config.block_align
    # Identical header (predictor/index) and identical first 24 nibble codes.
    # Byte-level agreement with real ffmpeg output is only possible with the
    # low-nibble-first packing.
    assert python_blocks[0][:16] == ffmpeg_blocks[0][:16]


def test_python_codec_blocks_decode_correctly_with_ffmpeg(sine_wav):
    path, samples = sine_wav
    config = talk_config()

    blocks = list(adpcm_blocks_from_file(path, config, codec="python"))
    wrapped = wrap_ima_wav(blocks, config.sample_rate, config.block_align, config.samples_per_block)
    decoded = ffmpeg_decode(wrapped)
    corr = correlation(decoded, samples)

    swapped = [swap_nibbles(block) for block in blocks]
    wrapped_swapped = wrap_ima_wav(swapped, config.sample_rate, config.block_align, config.samples_per_block)
    corr_swapped = correlation(ffmpeg_decode(wrapped_swapped), samples)

    assert corr > 0.99
    # With the nibble order reversed the very same data decodes measurably
    # worse, proving the low-first order is what ffmpeg's decoder expects.
    assert corr_swapped < 0.99
    assert corr_swapped < corr


# --- FIX 2: ffmpeg subprocess cleanup on early generator close -----------------


@pytest.mark.parametrize("codec", ["python", "ffmpeg"])
def test_generator_close_terminates_ffmpeg_promptly(codec, long_silence_wav, spy_popen):
    config = talk_config()
    generator = adpcm_blocks_from_file(long_silence_wav, config, codec=codec)
    block = next(generator)
    assert len(block) == config.block_align

    ffmpeg_processes = [process for process in spy_popen if "ffmpeg" in str(process.args[0])]
    assert len(ffmpeg_processes) == 1
    process = ffmpeg_processes[0]
    assert process.poll() is None  # ffmpeg still streaming: early-close case

    closer = threading.Thread(target=generator.close, daemon=True)
    started = time.monotonic()
    closer.start()
    closer.join(timeout=10.0)
    elapsed = time.monotonic() - started

    assert not closer.is_alive(), "generator.close() did not return (ffmpeg pipe deadlock)"
    assert elapsed < 8.0
    # Child must be reaped (no zombie) and its pipes closed.
    assert process.returncode is not None
    assert process.stdout is None or process.stdout.closed
    assert process.stderr is None or process.stderr.closed


def test_ffmpeg_codec_failure_still_reports_stderr(sine_wav):
    path, _ = sine_wav
    # block_align 132 is rejected by ffmpeg's adpcm_ima_wav encoder ("block
    # size must be power of 2"); the failure must surface as the informative
    # ffmpeg error, not be swallowed by the early-exit kill path.
    config = talk_config(length_per_encoder=256)

    with pytest.raises(RuntimeError, match="ffmpeg failed") as excinfo:
        list(adpcm_blocks_from_file(path, config, codec="ffmpeg"))
    assert "unexpected end of ffmpeg output" not in str(excinfo.value)


# --- FIX 3: transport servicing during unacked talk playback -------------------


def make_blocks(count: int, block_align: int) -> list[bytes]:
    return [b"\x00" * block_align for _ in range(count)]


def test_send_blocks_services_transport_without_wait_ack(monkeypatch):
    monkeypatch.setattr("pyneolink.voice.time.sleep", lambda seconds: None)
    camera = StubCamera()
    voice = Voice(camera)
    config = talk_config(length_per_encoder=256)  # 132-byte blocks, ~16 ms each

    blocks = make_blocks(64, config.block_align)  # ~1.03 s of audio
    voice._send_blocks(blocks, config, wait_ack=False)

    assert len(camera.sent) == len(blocks)  # every block sent exactly once
    assert camera.sock.maintain_calls >= 3
    assert camera.recv_calls >= 3


def test_send_blocks_survives_sock_without_maintain(monkeypatch):
    monkeypatch.setattr("pyneolink.voice.time.sleep", lambda seconds: None)
    camera = StubCamera(sock=object())
    voice = Voice(camera)
    config = talk_config(length_per_encoder=256)

    voice._send_blocks(make_blocks(32, config.block_align), config, wait_ack=False)

    assert len(camera.sent) == 32
    assert camera.recv_calls >= 1  # replies still drained


def test_send_blocks_wait_ack_path_unchanged(monkeypatch):
    monkeypatch.setattr("pyneolink.voice.time.sleep", lambda seconds: None)
    camera = StubCamera()
    voice = Voice(camera)
    config = talk_config(length_per_encoder=256)

    voice._send_blocks(make_blocks(20, config.block_align), config, wait_ack=True)

    assert len(camera.sent) == 20
    assert camera.recv_matching_calls == 20
    assert camera.sock.maintain_calls == 0
    assert camera.recv_calls == 0


# --- FIX 4: microphone respects duration when the stream produces no data ------


def test_microphone_exits_when_stream_is_silent_and_duration_elapses(monkeypatch):
    class FakeStream:
        def __init__(self, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    fake_sounddevice = types.ModuleType("sounddevice")
    fake_sounddevice.RawInputStream = FakeStream
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sounddevice)

    result: list[bytes] = []
    done = threading.Event()

    def consume() -> None:
        result.extend(adpcm_blocks_from_microphone(talk_config(), seconds=0.3))
        done.set()

    worker = threading.Thread(target=consume, daemon=True)
    started = time.monotonic()
    worker.start()
    assert done.wait(timeout=5.0), "microphone generator hung on an empty queue"
    assert time.monotonic() - started < 5.0
    assert result == []


# --- FIX 5: PCM stats skipped when debug output is disabled --------------------


def test_pcm_stats_not_observed_without_debug(monkeypatch, sine_wav):
    path, _ = sine_wav
    calls = []
    real_observe = internal_voice.observe_pcm_blocks

    def spy(blocks, stats):  # noqa: ANN001
        calls.append(stats)
        return real_observe(blocks, stats)

    monkeypatch.setattr(internal_voice, "observe_pcm_blocks", spy)

    blocks = list(adpcm_blocks_from_file(path, talk_config(), codec="python", on_debug=None))

    assert blocks
    assert calls == []


def test_pcm_stats_observed_with_debug(monkeypatch, sine_wav):
    path, _ = sine_wav
    calls = []
    real_observe = internal_voice.observe_pcm_blocks

    def spy(blocks, stats):  # noqa: ANN001
        calls.append(stats)
        return real_observe(blocks, stats)

    monkeypatch.setattr(internal_voice, "observe_pcm_blocks", spy)
    messages: list[str] = []

    blocks = list(adpcm_blocks_from_file(path, talk_config(), codec="python", on_debug=messages.append))

    assert blocks
    assert len(calls) == 1
    stats_lines = [message for message in messages if message.startswith("file pcm stats")]
    assert len(stats_lines) == 1
    assert "blocks=0 " not in stats_lines[0]
    assert "silent=False" in stats_lines[0]
