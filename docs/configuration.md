# Configuration reference

razorbill reads one TOML file: `~/.config/razorbill/config.toml`. Every
option is optional and the file may be absent entirely if `OPENAI_API_KEY`
is set in the environment. The path can be overridden with the
`RAZORBILL_CONFIG` environment variable, which is also how you point tests
or a second instance at a different setup.

Unknown keys are rejected at startup rather than ignored, so typos fail
loudly instead of silently doing nothing.

## API access

| option | default | notes |
|---|---|---|
| `api_key` | empty | Your OpenAI key. The environment variable `OPENAI_API_KEY` takes precedence over this. |
| `api_key_command` | empty | A shell command that prints the key, for example `pass show openai`. Used when neither the environment variable nor `api_key` is set. |
| `api_base` | `https://api.openai.com/v1` | Any OpenAI-compatible endpoint works. |
| `transcribe_model` | `gpt-4o-transcribe-diarize` | Needs segment timestamps to be useful. `whisper-1` also works and adds word-level timestamps. Models that return plain text with no timing degrade the transcript ordering. |
| `notes_model` | `gpt-5.5` | The chat model that writes the note. |
| `language` | empty | Optional ISO 639-1 hint for transcription, for example `en`. |
| `transcribe_api_base`, `transcribe_api_key` | empty | Override the endpoint or key for transcription only. Empty means use `api_base` and the main key. |
| `notes_api_base`, `notes_api_key` | empty | Same, for note generation. |

Splitting the two services is how you run Groq for audio and OpenAI for
notes, or a local transcription server with a cloud notes model.

## Output

| option | default | notes |
|---|---|---|
| `output_dir` | `~/Documents/meetings` | One Markdown file per meeting. In-progress audio lives under `.pending/` inside this directory. |
| `keep_audio` | `false` | When false, audio is deleted after transcription succeeds. Audio from failed runs is always kept so `razorbill reprocess` can retry. |
| `notify` | `true` | Desktop notifications through `notify-send`. |

## Detection and recording

| option | default | notes |
|---|---|---|
| `min_meeting_seconds` | `60` | Auto-detected recordings shorter than this are discarded as mic checks. Manual recordings are always kept. |
| `grace_seconds` | `20` | How long the microphone must be free before the meeting counts as over. |
| `poll_seconds` | `2.0` | Detection poll interval. |
| `max_hours` | `4.0` | Hard stop for a single recording. |
| `segment_seconds` | `600` | Audio chunk length sent to the transcription API. Smaller chunks transcribe sooner but with less context. |
| `ignore_apps` | `[]` | Extra application names whose mic use should not start a recording. Matched case-insensitively as substrings. razorbill's own plumbing, pavucontrol, and desktop settings panels are always ignored. |
| `source` | empty | Microphone device. Empty means the system default on Linux; required on macOS and Windows (see platform devices below). |
| `sink` | empty | System-audio device for the "Them" channel. On Linux, razorbill records this sink's monitor (default sink when empty). On macOS and Windows, empty disables the channel. |
| `echo_cancel` | `true` | Linux only: load PipeWire's echo-cancel module and route audio through it while the daemon runs, so speakers do not bleed into the mic. Defaults are restored on shutdown. Ignored elsewhere. |

## Platform devices

Automatic detection, default-device discovery, and echo cancellation are
Linux features. On macOS and Windows, recordings are started manually and
devices are named explicitly:

- macOS (avfoundation): list devices with
  `ffmpeg -f avfoundation -list_devices true -i ""`. Set `source` to the
  microphone's name or index. For the system-audio channel, install a
  loopback driver (for example BlackHole), route output through it, and set
  `sink` to its device name.
- Windows (dshow): list devices with
  `ffmpeg -list_devices true -f dshow -i dummy`. Set `source` to the
  microphone's device name. For system audio, set `sink` to a loopback
  capture device (Stereo Mix or virtual-audio-capturer, when available).

Without `sink`, the microphone channel alone is recorded and transcribed;
the diarizing model still labels the speakers it hears.

## Live mode

| option | default | notes |
|---|---|---|
| `live_transcript` | `false` | Maintain a rolling transcript in `live.md` in the meeting directory during the meeting. |
| `live_mode` | `realtime` | `realtime` streams audio to the provider's `/v1/realtime` WebSocket; lines land within seconds. `deepgram` streams to Deepgram instead and adds interim results mid-sentence. `segments` batch-transcribes each finished segment (lag up to one segment length; results cached in `live.json` and reused by final processing). |
| `realtime_model` | `gpt-realtime-whisper` | Transcription model for the `realtime` stream. |
| `deepgram_api_key` | empty | Required for `live_mode = "deepgram"`. The live stream is the only thing it is used for. |
| `deepgram_model` | `nova-3` | Deepgram model for the live stream. |
| `deepgram_diarize` | `true` | Voice diarization on the live stream: multiple remote speakers become Them (A), Them (B). Channel attribution (Me vs Them) is always on when a system-audio device exists. |
| `live_insights` | `false` | Copilot pass on every live utterance: suggested answers, one-line explanations of things just mentioned, follow-up questions, or silence. Passes coalesce (one in flight, latest transcript wins), so cost scales with conversation activity. Needs `live_transcript`. |
| `insight_model` | empty | Chat model for copilot passes. Empty uses `notes_model`; a smaller model such as `gpt-5.4-mini` roughly halves pass time with little quality loss on one-line tips. |
| `insight_priority` | `false` | Request OpenAI's priority service tier for copilot passes. Costs more; only worth testing if copilot latency matters to you. |
| `silence_stop_minutes` | `10.0` | Backstop: end the meeting after this long with no speech in the live transcript, with an actionable warning notification at 3 minutes. Covers apps that keep actively capturing after a call ends (paused mic streams already do not count as meetings). After a silence stop, no new recording starts until the mic is released once. `0` disables. |
| `context_dirs` | `[]` | Directories of `.md`/`.txt` background documents used by note generation, `razorbill ask`, and insights. Under about 40 KB total they are injected whole; above that, a selection call picks up to six relevant files from an index. Hidden directories are skipped. |
| `calendar_ics_url` | empty | Read-only ICS feed for calendar awareness (Google Calendar: Settings, your calendar, "Secret address in iCal format"; Outlook: "Publish calendar"). At recording start the current event's title, attendees, and description are resolved (feed cached 15 minutes) and ground the copilot, `ask`, note generation (including the title), and background-document selection. Common recurrence rules are supported; moved single instances of a series are not. |

## Notes

| option | default | notes |
|---|---|---|
| `notes_prompt_file` | empty | Path to a file that replaces the built-in note-writing prompt. Use this for your own template, a standup format, another language, and so on. The calendar event, background documents, and transcript are appended after the prompt. |

The built-in prompt asks for a title line, a summary, key points, decisions,
action items as checkboxes, and open questions.
