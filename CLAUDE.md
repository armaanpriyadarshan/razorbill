# razorbill

Meeting transcription and notes from system audio. Python 3.11+, package
name `razorbill-notes`, command `razorbill`.

## Commands

- `uv sync` sets up the dev environment, `uv run pytest` runs tests,
  `uv build` builds sdist and wheel.
- `razorbill status --json` prints daemon state; `start`/`stop`/`toggle`/
  `note "text"` control it non-interactively; `last` prints the newest
  note path. Notes are Markdown files in `output_dir`
  (default `~/Documents/meetings`).

## Architecture

One module per concern, all under `razorbill/`:

- `config.py`: TOML config dataclass; unknown keys are fatal.
- `audio.py`: capture and detection. Platform backends: pulse (Linux,
  auto-detection + echo cancel), avfoundation (macOS), dshow (Windows).
  One ffmpeg process per channel.
- `daemon.py`: watch loop; writes `status.json` each tick; spawns a
  processing thread per finished meeting.
- `state.py`: file-based IPC in `$XDG_RUNTIME_DIR/razorbill/` shared by
  daemon, CLI, and TUI (status.json, start-request and stop marker files).
- `openai_api.py`: stdlib HTTP client for `/audio/transcriptions` and
  `/chat/completions`; per-service endpoint overrides.
- `transcript.py`: merge the me/them channels by timestamp, drop silence
  hallucinations and echo duplicates.
- `meeting.py`: post-meeting pipeline (transcribe, merge, generate,
  write); `.pending/` claim protocol for crash-safe retries.
- `tui.py`: Textual app. Reads the same files as the CLI; no daemon
  coupling.

## Conventions

- The daemon path uses only the standard library; Textual is imported
  lazily by the TUI command alone.
- Docs and user-facing strings are plain and factual. No marketing
  language, no em dashes.
- Every subprocess call has a timeout and failure path; the daemon must
  survive any API or audio failure (log, notify, keep audio, continue).
- Test before committing: `uv run pytest`.
