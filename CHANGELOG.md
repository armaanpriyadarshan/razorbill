# Changelog

## 0.8.0 (2026-07-07)

- Deepgram live mode (`live_mode = "deepgram"`, needs `deepgram_api_key`):
  audio streams to Deepgram's `/v1/listen` and interim results arrive while
  a sentence is still being spoken (first partial about 300 ms after speech
  starts in testing; finalized utterances land within a second of a pause).
  The copilot reacts to partials mid-utterance, so it can prepare an answer
  while the question is being asked. OpenAI realtime stays the default.
- The audio-mixing ffmpeg helper moved to `audio.mixed_pcm`, shared by both
  streaming adapters. The WebSocket client sends binary frames.

## 0.7.1 (2026-07-06)

- Live latency work. Utterance flush now runs on a 1 second receive tick
  with a 1.2 second idle window (finished utterances reached `live.md`
  around 2 seconds after speech ends in testing, down from about 6). The
  WebSocket frame parser consumes nothing until a frame is fully buffered,
  so the short tick cannot corrupt the stream.
- Copilot passes send a trimmed prompt (8 KB document budget, 4 KB
  transcript tail); measured pass time dropped from about 4.5 to 2.6
  seconds on the default model.
- `insight_priority` requests OpenAI's priority service tier for copilot
  passes (higher price; made no measurable difference at this payload size
  in testing, so it is off by default).

## 0.7.0 (2026-07-06)

- The insight interval is gone. The live copilot now runs on every
  utterance as it lands in the transcript: passes coalesce (one in flight;
  a burst of utterances yields one rerun over the newest state), so the
  only pacing is model latency.
- The copilot prompt targets in-call help: suggested answers to questions
  just asked, one-line explanations of companies and tools grounded in the
  background documents, naming the play the other side is running, and
  follow-up questions.
- `insight_model` selects a faster chat model for copilot passes; unset,
  they use `notes_model`. Background-document selection is cached per
  meeting and refreshed as the conversation grows, keeping the
  per-utterance path to a single chat call. `insight_interval` is removed.

## 0.6.0 (2026-07-06)

- Realtime live transcript (`live_mode = "realtime"`, the default): audio
  streams to the provider's realtime WebSocket and transcript lines land in
  `live.md` within seconds of the words being spoken. Stdlib WebSocket
  client; automatic reconnect with backoff. The previous per-segment
  implementation remains available as `live_mode = "segments"`.
- Proactive insight passes are rate-limited by `insight_interval` (default
  60 seconds) instead of running once per segment.

## 0.5.0 (2026-07-06)

- Live mode (`live_transcript`): segments are transcribed during the
  meeting into a rolling `live.md`, cached so final processing never
  re-bills them.
- `razorbill ask "..."` and the `a` key in the TUI: questions answered
  against the live transcript during a meeting, or the most recent note
  after one.
- Proactive insights (`live_insights`): after each live segment the model
  surfaces at most two new items worth interrupting with, or stays silent.
  Delivered as notifications, in the TUI, and in `insights.md`.
- `context_dirs`: directories of Markdown or text background documents
  that ground note generation, ask, and insights, with an index-based
  selection step for large collections.

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
- Cross-platform recording: automatic detection, default devices, and echo
  cancellation on Linux (PipeWire); manual recording with explicitly
  configured devices on macOS (avfoundation) and Windows (dshow).
  Notifications via notify-send on Linux and osascript on macOS.
- `razorbill status --json` for scripts and agents. `razorbill --version`.
- Assets generated from a razorbill reference photograph: the logo through
  the OpenAI image API, the terminal art through ascii-image-converter.

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
