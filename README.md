<p align="center">
  <img src="assets/razorbill.png" width="112" alt="razorbill logo">
</p>

<h1 align="center">razorbill</h1>

Meeting transcription and notes from system audio. Bring your own
OpenAI-compatible API key. TUI, CLI, and a background daemon; notes are
plain Markdown files.

<p align="center">
  <img src="assets/tui-main.svg" width="720" alt="razorbill TUI">
</p>

## How it works

- The daemon records two channels: the microphone ("Me") and system audio
  ("Them"). On Linux, recording starts automatically when another
  application opens the microphone (every meeting app does) and stops when
  the microphone has been free for `grace_seconds`. On macOS and Windows,
  recording is started manually from the TUI or CLI.
- After the meeting: audio segments are transcribed in parallel
  (`gpt-4o-transcribe-diarize` by default, so remote speakers are labeled
  "Them (A)", "Them (B)"), merged by timestamp, and passed to a chat model
  (`gpt-5` by default) that writes the note: title, summary, decisions,
  action items, open questions, full transcript.
- Output is one Markdown file per meeting in `output_dir`
  (`~/Documents/meetings` by default), with YAML frontmatter. Audio is
  deleted after successful transcription; on failure it is kept and
  `razorbill reprocess` retries.
- Notes you jot during a meeting (TUI, `razorbill note`, or a hotkey) are
  passed to the model as anchors to expand.
- On Linux the daemon loads PipeWire's echo-cancel module so speakers do
  not bleed into the microphone; transcript-level deduplication removes
  any remaining bleed.

## Install

```sh
uv tool install razorbill-notes    # or: pipx install razorbill-notes
```

The installed command is `razorbill`. (The name `razorbill` on PyPI belongs
to an unrelated package.) From source: `git clone
https://github.com/armaanpriyadarshan/razorbill && cd razorbill && uv tool
install .`

Requirements: Python 3.11+, `ffmpeg` on PATH. Linux additionally needs
PipeWire or PulseAudio (`pactl`) and, optionally, `libnotify`.

## Setup

```sh
razorbill
```

First run prompts for an API key, verifies it, and writes it to
`~/.config/razorbill/config.toml` (mode 600). Alternatives: the
`OPENAI_API_KEY` environment variable, or `api_key_command = "pass show
openai"` for a secret manager.

Run the daemon in the foreground with `razorbill run`, or as a service.
Linux (systemd):

```sh
cp razorbill.service ~/.config/systemd/user/
systemctl --user enable --now razorbill
```

## Usage

`razorbill` opens the TUI: live daemon status, a jot box during recording,
and the note list with a built-in Markdown reader.

| key | action |
|---|---|
| `enter` / `e` | read note / open in editor |
| `n` | jot into the live meeting |
| `r` | start or stop recording |
| `p` | retry failed processing |
| `q` | quit |

CLI: `status [--json]`, `statusline [--polybar]`, `toggle`, `start`,
`stop`, `note "..."`, `last`, `reprocess`, `run`, `bird`.

## Platform support

| capability | Linux | macOS | Windows |
|---|---|---|---|
| automatic meeting detection | yes | no | no |
| manual recording (TUI/CLI) | yes | yes | yes |
| system-audio ("Them") channel | yes | via loopback device | via capture device |
| echo cancellation | yes | no | no |
| notifications | notify-send | osascript | no |
| TUI, notes, configuration | yes | yes | yes |

On macOS and Windows, name the capture devices in the config: `source` is
the microphone, `sink` is an optional system-audio device (macOS: a
loopback driver such as [BlackHole](https://github.com/ExistentialAudio/BlackHole);
Windows: a loopback capture device such as Stereo Mix or
virtual-audio-capturer). List devices with `ffmpeg -f avfoundation
-list_devices true -i ""` (macOS) or `ffmpeg -list_devices true -f dshow -i
dummy` (Windows). Without `sink`, razorbill records the microphone only;
with the diarizing model, speakers picked up through the microphone are
still labeled. macOS and Windows support is newer than the Linux path and
has seen less testing.

## Configuration

One TOML file; every option has a default. See
[config.example.toml](config.example.toml) and
[docs/configuration.md](docs/configuration.md). Commonly changed:
`transcribe_model`, `notes_model`, `output_dir`, `ignore_apps`,
`notes_prompt_file`. Transcription and note generation can use different
OpenAI-compatible endpoints (`transcribe_api_*`, `notes_api_*`), which
covers Groq and local transcription servers.

## Scripting and agents

Everything razorbill knows is a file or a one-shot command, which makes it
easy to drive from scripts and coding agents such as Claude Code:

- `razorbill status --json` prints daemon state
  (`{"state": "recording", "app": ..., "since": ...}`).
- Notes are Markdown files with YAML frontmatter in one directory; reading,
  searching, and summarizing them requires no API.
- `razorbill start`, `stop`, `toggle`, and `note "text"` are non-interactive
  and exit non-zero on failure.
- `razorbill last` prints the path of the newest note.

Example: "summarize this week's meetings" is `ls ~/Documents/meetings` plus
reading the files.

## Desktop integration (optional)

On Linux, notifications carry actions (Stop while recording, Open when
notes are ready). For bars and hotkeys:

```ini
; polybar
[module/razorbill]
type = custom/script
exec = ~/.local/bin/razorbill statusline --polybar
interval = 2
click-left = ~/.local/bin/razorbill toggle
click-right = ~/.local/bin/razorbill last
```

```json
// waybar
"custom/razorbill": {
    "exec": "~/.local/bin/razorbill statusline",
    "interval": 2,
    "on-click": "~/.local/bin/razorbill toggle"
}
```

```
# i3: jot hotkey via dmenu
bindsym $mod+r exec --no-startup-id sh -c 'j=$(dmenu -p "jot:" < /dev/null); [ -n "$j" ] || exit 0; ~/.local/bin/razorbill note "$j" || notify-send --app-name=razorbill "razorbill" "No meeting is being recorded"'
```

## Privacy

Audio goes to the configured transcription endpoint; transcript text goes
to the configured notes endpoint. With defaults, both are api.openai.com
under your key. Local audio is deleted after transcription
(`keep_audio = false`). No telemetry. Recording calls is regulated in many
jurisdictions; know your local rules.

## Development

```sh
uv sync && uv run pytest
uv build
```

One module per concern: `audio.py` (capture, detection, platform
backends), `daemon.py` (watch loop), `openai_api.py` (stdlib HTTP client),
`transcript.py` (merge, echo dedup), `meeting.py` (post-meeting pipeline),
`tui.py` (Textual interface), `state.py` (file-based IPC). The daemon
imports nothing outside the standard library; Textual is needed only for
the TUI.

Named after the razorbill (Alca torda). Logo generated from a reference
photograph; `razorbill bird` prints an ASCII conversion of the same photo.

## License

MIT.
