# Changelog

## Unreleased

Protocol-review fixes: streaming stability, transport correctness, and module hardening.

### Fixed

- Removed the stream keepalive feedback loop: keepalive replies from the camera are no longer answered (only camera-initiated requests are), and live streams/recordings now ping with `MSG.PING` (93) every 5 s instead of `MSG.UDP_KEEPALIVE` (234) every 0.75 s, which modern firmware rejects with 405. This eliminates ~900 junk messages per second during streaming.
- TCP connections no longer desynchronize permanently when a socket timeout interrupts a partially received Baichuan message; `recv_message` now uses a persistent per-connection receive buffer.
- UDP transport no longer re-stores duplicate packets that were already consumed, which leaked memory and could spuriously trip the `max_pending_chunks` overflow during long lossy sessions.
- MPEG-TS timestamps no longer jump backwards when the camera's 32-bit microsecond counter wraps (~71.6 min); the muxer unwraps deltas and clamps implausible gaps, keeping PTS monotonic for direct TS, HLS, and recordings.
- Camera-supplied snapshot file names are sanitized to their final path component, closing a path-traversal hole when saving to a directory.
- ADPCM talk audio from the pure-Python encoder now packs the first sample of each pair into the low nibble (matching ffmpeg/gstreamer/neolink); previously speech played distorted.
- Voice/ffmpeg block generators kill and reap the ffmpeg child when closed early instead of deadlocking on a full pipe.
- Talk playback services the transport while pacing audio (drains replies, processes UDP ACKs, sends heartbeats), preventing unbounded resend backlogs and relay session timeouts.
- Motion listeners release the camera online lease when startup fails, and `status()` always cleans up; a leaked lease used to silently flip battery polling out of reconnect mode permanently.
- IR/PIR setters raise `TimeoutError` when the camera never confirms a set instead of silently reporting success.
- SD-card downloads are bounded to a fixed number of passes instead of retrying size mismatches forever; abandoned download strategies are stopped on the correct playback channel, drained, and their late data can no longer interleave into the next strategy's file.
- SD-card downloads honor the startup idle window before abandoning a strategy, discard their `binary_msg_nums` entries afterwards, clean up non-empty `.part` files, and force a reconnect after raw-tail recovery so the shared transport is never reused desynchronized.
- `SdCard.list(file_type=...)` is now actually sent to the camera instead of being silently ignored.
- `ConnectionState` writes atomically (temp file + rename) and merges with the on-disk state on save, so concurrent cameras no longer erase each other's cached addresses; corrupt or non-dict state files degrade gracefully.
- CLI: `--camera` given before the subcommand is honored; `--info` no longer overrides an explicitly requested subcommand; `discover` without `--uid` no longer sends a bogus `uid=None` query to Reolink's P2P servers.
- Config loading dispatches by suffix (`.json`/`.toml`), falls back to trying both with a clear error, and reports a missing camera `name` as `ValueError` instead of a bare `KeyError`.
- `Camera._recv` raises `TimeoutError` for non-positive timeouts instead of putting TCP sockets into non-blocking mode (`BlockingIOError` escapes and possible desync).
- Media parser resyncs immediately on oversized/corrupt frame headers instead of buffering unbounded data, retries parsing in the same round after a resync, and preserves a magic prefix split across chunk boundaries.

### Changed

- `Camera` login/receive state (`binary_msg_nums`, receive buffer) is cleared on `close()`; send and receive paths are guarded by locks for background recorder/preview threads.
- HLS sessions stop the camera stream after `hls_idle_seconds` (default 60 s) without viewers — important for battery cameras — and restart cleanly on the next request; stream startup fails with a clear timeout instead of hanging when the camera never delivers a keyframe.
- Login with `max_encryption="bc"`/`"none"` against modern firmware now reports a clear protocol error instead of an opaque `EOFError`.
- Default SD-card preview cache files live in the system temporary directory with unique names instead of a CWD-relative `.tmp/` folder.
- `SdCard.remove()` raises `NotImplementedError` immediately instead of demanding confirmation for an unimplemented operation.

---

## 0.4.0

SD-card file API and preview playback work.

### Added

- Added `SDFile` wrappers for SD-card recordings with `info()`, `download()`, and `preview()`.
- Added `SdCard.files()` and `SdCard.file(...)` helpers.
- Added cached SD-card preview playback with an HTTP stream helper for players such as VLC.
- Updated `examples/sd_card_example.py` with list, download, preview, remove, and format examples.

### Changed

- Moved public recording downloads from `SdCard.download(file, ...)` to `SDFile.download(...)`.
- Updated README, docs, and examples to use the new SD-card file API.
- Use camera `file_name` plus the media extension for finalized download filenames.

### Fixed

- Treat camera `400` responses after partial SD-card download data as interrupted downloads so reconnect/retry handling can recover.

---

## 0.3.2

Downloader reliability improvements.

### Added

- Added `CameraConnectionError` for unrecoverable camera reconnect failures.
- Added `reconnect_retries` to `SdCard.download()` for interrupted long downloads.
- Added `rewrite_exists` to `SdCard.download()` to skip already finalized local files.
- Added IDE-friendly docstrings for SDK classes, CLI helpers, and core protocol components.

### Changed

- Treat existing non-empty `.mp4` files as complete when `rewrite_exists=False`.
- Remove stale `.part` files for a recording when the finalized `.mp4` is skipped.
- Translated internal documentation to English for publication.

---

## 0.3.1

PyPI metadata and README link update.

### Changed

- Updated package metadata and installation links for the first PyPI publication.

---

## 0.3.0

Initial public alpha preparation.

### Added

- UID/P2P camera connection with local, relay, and cached address paths.
- Baichuan login, command framing, BC XOR, and AES-CFB support.
- Camera info, UID, reboot, snapshot, LED/IR compatibility commands.
- SD-card listing with pagination and high/low recording download.
- Battery status with reconnect and online polling modes.
- Live MPEG-TS stream server and HLS timeshift buffer.
- Local MPEG-TS recording from live streams.
- Motion status and motion event watch mode.
- Voice/talk from microphone, audio file, or generated tone.
- Built-in siren trigger.
- Settings facade with PIR and IR light controls.
- CLI and SDK examples.

### Notes

- This release is experimental and reverse engineered.
- Tested on a limited number of Reolink cameras.
- API compatibility is not guaranteed before `1.0.0`.
