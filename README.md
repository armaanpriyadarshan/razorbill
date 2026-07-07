<p align="center">
  <img src="assets/razorbill.svg" width="128" alt="A geometric razorbill swimming">
</p>

<h1 align="center">razorbill</h1>

<p align="center">Meeting notes that write themselves.</p>

razorbill is a small daemon for Linux that notices when a meeting starts,
records it from system audio, and writes a Markdown note to a folder a minute
or so after you hang up. It never joins a call as a participant. There is no
bot in the attendee list and nothing for the other side to see. You bring your
own OpenAI key and the notes land on your own disk.

It was born out of wanting [Granola](https://granola.ai) on Linux, where
Granola does not run, and out of stubbornness about keeping the whole thing
small. The daemon is plain Python on top of `ffmpeg` and `pactl`. The core has
no dependencies at all; the terminal interface uses
[Textual](https://textual.textualize.io/).

<p align="center">
  <img src="assets/tui-main.svg" width="720" alt="The razorbill TUI: recording status, jot box, meeting list">
</p>

## What it does

Every meeting app opens your microphone for the length of the call. Zoom,
Meet or Teams in a browser tab, Slack huddles, Discord: all of them. razorbill
watches PipeWire for any foreign app capturing the mic and treats that as the
start of a meeting. When the mic has been quiet for twenty seconds, the
meeting is over. This works with every platform and needs neither a calendar
nor a per-app integration.

While the meeting runs, razorbill records two channels: your microphone
("Me") and the system audio monitor, which is everything you hear ("Them").
Speaker attribution comes free from that split. Afterward the audio goes to
the transcription API in parallel chunks, the two transcripts get interleaved
by timestamp, and a language model turns the result into a note: title,
summary, decisions, action items as checkboxes, open questions, and the full
transcript underneath. If you jotted anything during the call, each jot
becomes an anchor that the model expands with detail from the transcript.
That idea is stolen from Granola, with credit; it is their best feature.

The note is a plain Markdown file with YAML frontmatter. Grep it, sync it,
point Obsidian at the folder. The audio is deleted as soon as transcription
succeeds, and kept only if something failed, so `razorbill reprocess` can
retry.

## Install

You need Linux with PipeWire or PulseAudio, `ffmpeg`, and Python 3.11 or
newer. `notify-send` (libnotify) is optional but recommended.

```sh
uv tool install razorbill-notes     # or: pipx install razorbill-notes
```

The installed command is `razorbill`. (The bare name `razorbill` on PyPI
belongs to an unrelated project, hence the longer distribution name.)

From a checkout:

```sh
git clone https://github.com/armaanpriyadarshan/razorbill && cd razorbill
uv tool install .
```

## First run

```sh
razorbill
```

That opens the terminal interface. On first run it asks for your OpenAI API
key, checks it against the API, and stores it in
`~/.config/razorbill/config.toml` with owner-only permissions. If you would
rather not keep the key in a file, set `OPENAI_API_KEY` in the environment or
point `api_key_command` at your secret manager; see
[docs/configuration.md](docs/configuration.md).

Then start the daemon and forget about it:

```sh
mkdir -p ~/.config/systemd/user
cp razorbill.service ~/.config/systemd/user/
systemctl --user enable --now razorbill
```

`razorbill run` starts the same daemon in the foreground if you want to watch
it work first. Join any call and it goes: a notification when recording
starts, another when the note is ready.

## The terminal interface

`razorbill` (or `razorbill tui`) works over SSH, inside tmux, and in any
terminal that can show colors, including the terminal inside coding agents.

| key | action |
|---|---|
| `enter` | read the selected note, rendered |
| `e` | open the selected note in your editor |
| `n` | jot a note into the live meeting |
| `r` | start recording now, or stop the current one |
| `p` | retry meetings whose processing failed |
| `q` | quit |

The status line at the top is live: a quiet circle while waiting, a red dot
with the app name and elapsed minutes while recording, a pen while notes are
being written. The jot box only appears while a meeting is running.

The TUI is a viewer and remote control. Closing it changes nothing about the
daemon, which keeps recording meetings on its own.

## How it works

Four moving parts, each replaceable:

1. Detection polls `pactl list source-outputs` every two seconds and looks
   for a real application capturing a real microphone. Monitor streams,
   audio filters, and razorbill's own recorder are filtered out. An app
   list in the config (`ignore_apps`) handles things like OBS.
2. Recording is two `ffmpeg` processes writing 16 kHz mono Opus in ten
   minute segments. Two processes rather than one, so a channel that stops
   producing data can never stall the other.
3. Echo cancellation keeps speakers usable instead of forcing headphones.
   The daemon loads PipeWire's echo-cancel module on startup and routes
   audio through it, then restores your defaults on exit. A second guard
   runs at the text level: transcript segments that appear in both channels
   at the same moment are speaker bleed, and the copy on the mic channel
   gets dropped.
4. Processing transcribes all segments in parallel, merges the channels by
   timestamp, filters silence hallucinations, and asks the notes model for
   the write-up. Everything after the meeting is one retryable batch job,
   which is what keeps this project a few files instead of a streaming
   audio pipeline with a GPU build matrix.

## Choosing a transcription model

The default is OpenAI's `gpt-4o-transcribe-diarize`: more accurate than
`whisper-1` at the same price, and it labels speakers, so several people on
the far side show up as "Them (A)" and "Them (B)" instead of one blob.

| goal | how |
|---|---|
| best notes with one OpenAI key | the default |
| word-level timestamps | `transcribe_model = "whisper-1"` |
| about nine times cheaper and much faster | Groq: `transcribe_api_base = "https://api.groq.com/openai/v1"`, `transcribe_model = "whisper-large-v3-turbo"`, plus a Groq key |
| audio never leaves the machine | run a local server with an OpenAI-compatible endpoint (for example [achetronic/parakeet](https://github.com/achetronic/parakeet)) and point `transcribe_api_base` at it |

Transcription and note generation can use different providers. The notes
model defaults to `gpt-5`.

## Desktop integration

None of this section is required. On a stock GNOME or KDE desktop, razorbill
already does everything through notifications and the TUI; there is nothing
to set up. The recipes below are for people who run their own bar or window
manager and want the state surfaced there. The building blocks are generic:
`statusline` prints one line of state for any bar, `toggle` and `last` are
made for click and hotkey bindings, and notifications carry actions where
the notification daemon supports them (Stop on the recording toast, Open on
the notes-ready one; dunst maps these to middle-click).

Waybar:

```json
"custom/razorbill": {
    "exec": "~/.local/bin/razorbill statusline",
    "interval": 2,
    "on-click": "~/.local/bin/razorbill toggle",
    "on-click-right": "~/.local/bin/razorbill last"
}
```

Polybar:

```ini
[module/razorbill]
type = custom/script
exec = ~/.local/bin/razorbill statusline --polybar
interval = 2
click-left = ~/.local/bin/razorbill toggle
click-right = ~/.local/bin/razorbill last
```

i3, a jot hotkey through dmenu:

```
bindsym $mod+r exec --no-startup-id sh -c 'j=$(dmenu -p "jot:" < /dev/null); [ -n "$j" ] || exit 0; ~/.local/bin/razorbill note "$j" || notify-send --app-name=razorbill "razorbill" "No meeting is being recorded"'
```

CLI: `status`, `statusline [--polybar]`, `toggle`, `last`, `note "..."`,
`start`, `stop`, `reprocess`.

## Configuration

Everything lives in one file, `~/.config/razorbill/config.toml`, and every
option has a sensible default. [config.example.toml](config.example.toml) is
a commented copy; [docs/configuration.md](docs/configuration.md) documents
each option. The ones people actually change: `notes_model`,
`transcribe_model`, `output_dir`, `ignore_apps`, and `notes_prompt_file` for
a custom note template.

## Platform support

| part | Linux | macOS | Windows |
|---|---|---|---|
| recording daemon | yes | no | no |
| TUI, notes browsing, configuration | yes | yes | yes |

The recording side is built on PipeWire and PulseAudio, so it is Linux-only
today. Capture on macOS (ScreenCaptureKit) and Windows (WASAPI loopback) is
possible and the recorder is isolated in one module, but I have not written
it. The TUI and everything downstream of a recorded meeting run anywhere
Python runs, which also means you can read your notes on a laptop that never
records anything.

## Privacy

Worth stating plainly:

- Meeting audio goes to your configured transcription endpoint, and the
  transcript text goes to your configured notes endpoint. With the defaults,
  both are api.openai.com under your own key and their API data-use terms.
- Local audio is deleted right after successful transcription. Set
  `keep_audio = true` if you want the recordings.
- The API key file is written with mode 600. Nothing phones home; there is
  no telemetry of any kind.
- Recording without the other side's knowledge is regulated in many places.
  Know your local rules and your company's, and tell people when that is
  the decent thing to do.

## Limitations

- If you switch audio devices mid-meeting, the recording stays on the
  devices that were default when it started.
- Without the diarizing model, everyone on the far side is a single "Them".
- Echo cancellation changes your default sink and source to razorbill's
  virtual pair while the daemon runs. If your setup already handles echo
  some other way, set `echo_cancel = false`.
- A meeting where nobody speaks produces a note that honestly says so.

## Development

```sh
uv sync          # dev environment
uv run pytest    # tests
uv build         # sdist + wheel
```

The layout is one module per concern: `audio.py` talks to PipeWire and
ffmpeg, `daemon.py` owns the watch loop, `openai_api.py` is a small stdlib
HTTP client, `transcript.py` merges and de-echoes, `meeting.py` runs the
post-meeting pipeline, `tui.py` is the interface, `state.py` is the
file-based IPC that everything shares. The daemon deliberately imports
nothing outside the standard library.

## The name

The [razorbill](https://en.wikipedia.org/wiki/Razorbill) is a North Atlantic
auk: black above, white below, with one sharp white stripe across its bill.
It sits quietly on the water, dives when there is something worth diving
for, and comes back up with the goods. That seemed like the right bird for
a program that sits in the corner of your desktop and surfaces with notes.

## License

MIT. See [LICENSE](LICENSE).
