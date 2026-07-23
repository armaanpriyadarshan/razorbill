# razorbill

Meeting transcription, notes, and screen recording from system audio.
Python 3.11+, package name `razorbill-notes`, command `razorbill`.

## Commands

- `uv sync` sets up the dev environment, `uv build` builds sdist and
  wheel. The test suite is not part of the public repository.
- `razorbill status --json` prints daemon state; `start`/`stop`/`toggle`
  control it non-interactively; `ask "..."` answers one question over the
  live transcript or latest note; `last` prints the newest note path.
  Notes are Markdown files in `output_dir`
  (default `~/Documents/meetings`).

## Architecture

One module per concern, all under `razorbill/`:

- `config.py`: TOML config dataclass; unknown keys are fatal.
- `audio.py`: capture and detection. Platform backends: pulse (Linux,
  auto-detection + echo cancel), avfoundation (macOS), dshow (Windows).
  One ffmpeg process per recorded channel; `mixed_pcm` mixes both into
  one PCM stream for the live transcription adapters.
- `video.py`: meeting screen recording. One ffmpeg captures the full
  screen (x11grab on Linux, avfoundation screen device on macOS), video
  only, into `screen.mkv`; a pulse monitor with an idle sink delivers no
  data and would stall a live audio mux. `mux()` merges the recorded
  audio segments in at processing time; `meeting.py` moves the result
  next to the note.
- `daemon.py`: watch loop; writes `status.json` each tick; spawns a
  processing thread per finished meeting.
- `ws.py`: minimal stdlib WebSocket client (wss, resumable frame parser,
  text and binary frames).
- `realtime.py`: streaming transcription over OpenAI `/v1/realtime`.
  Utterances are assembled from deltas; a "completed" event is not
  guaranteed, so flushes happen on item change, idle, and shutdown.
- `deepgram.py`: streaming transcription over Deepgram `/v1/listen` with
  interim results. Stereo multichannel when a system-audio device exists
  (mic left, system right) for live Me/Them labels, plus voice diarization
  for Them (A)/(B); `handle()` is pure and unit-tested.
- `state.py`: file-based IPC in `$XDG_RUNTIME_DIR/razorbill/` shared by
  daemon, CLI, and TUI (status.json, start-request and stop marker files).
- `openai_api.py`: stdlib HTTP client for `/audio/transcriptions` and
  `/chat/completions`; per-service endpoint overrides.
- `context.py`: background-document injection from `context_dirs`; small
  collections go in whole, large ones through index-based selection.
- `ask.py`: prompts and assembly for `razorbill ask`; grounded in
  `context.py` output.
- `events.py`: ICS calendar feed parsing and current-event resolution
  (common recurrence rules only); the event grounds ask, notes,
  and document selection.
- `transcript.py`: merge the me/them channels by timestamp, drop silence
  hallucinations and echo duplicates.
- `meeting.py`: post-meeting pipeline (transcribe, merge, generate,
  write); `.pending/` claim protocol for crash-safe retries; `live.json`
  segment cache shared with segments-mode live transcription.
- `tui.py`: Textual app. Reads the same files as the CLI; no daemon
  coupling.

Live-meeting artifacts inside a meeting directory: `live.md` (rolling
transcript), `screen.mkv` (screen recording), `live.json` (segment cache,
segments mode only).

## Conventions

- The daemon path uses only the standard library; Textual is imported
  lazily by the TUI command alone.
- Docs and user-facing strings are plain and factual. No marketing
  language, no em dashes.
- Every subprocess call has a timeout and failure path; the daemon must
  survive any API or audio failure (log, notify, keep audio, continue).
- Video capture is best-effort; its failure never ends or blocks a meeting.
