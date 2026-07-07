# Changelog

## 0.4.0 (2026-07-06)

- Renamed from razorbird to razorbill, which is what the bird is actually
  called. The distribution name on PyPI is `razorbill-notes` because the
  bare name belongs to an unrelated package; the command, module, config
  directory (`~/.config/razorbill`), and systemd unit are all `razorbill`.
- `razorbill bird` prints a full ASCII portrait of the bird, converted from
  a photograph.
- New terminal interface (`razorbill`, or `razorbill tui`): live status,
  meeting list, rendered note view, jot box, record toggle, reprocess.
  Built on Textual; runs in any terminal, including over SSH.
- First-run setup in the TUI: paste your OpenAI key, it is verified against
  the API and written to the config with mode 600.
- Publishing groundwork: full package metadata, MIT license file, tests
  (pytest), this changelog, a logo, and documentation under `docs/`.
- The daemon now refuses to start on non-Linux platforms with a clear
  message instead of failing inside pactl. The TUI and CLI run anywhere.
- `razorbill --version`.

## 0.3.1 (2026-07-06)

- Manual recordings are always kept; the minimum-length discard now only
  applies to auto-detected recordings, and discards produce a notification.
- The i3 jot binding uses dmenu.

## 0.3.0 (2026-07-06)

- Web UI removed. The interface is now the desktop itself: actionable
  notifications (Stop while recording, Open when notes are ready), a
  polybar module (`razorbill statusline --polybar`), and `toggle` and
  `last` commands for bar clicks.
- Default transcription model is `gpt-4o-transcribe-diarize`: better
  accuracy than whisper-1 at the same price, plus speaker labels for the
  Them channel ("Them (A)", "Them (B)").
- Transcription and notes can use different OpenAI-compatible endpoints
  (`transcribe_api_*`, `notes_api_*`), which makes Groq and local servers
  drop-in options.
- Echo cancellation through PipeWire's echo-cancel module, managed by the
  daemon, so recording without headphones works. A transcript-level echo
  dedup backs it up.
- The recorder is two independent ffmpeg processes, so a stalled channel
  cannot take down the other one.

## 0.2.0 (2026-07-06)

- Web UI on localhost: status, jot box, meeting list, rendered notes.
- Meeting-end race fixes: the recorder is excluded from detection by name
  and PID; processing claims prevent double-processing between the daemon
  and `reprocess`.

## 0.1.0 (2026-07-06)

- First working version: PipeWire mic-use detection, two-channel Opus
  recording, batch transcription through whisper-1, note generation, plain
  Markdown output, systemd unit, CLI.
