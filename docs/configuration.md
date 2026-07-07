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
| `notes_model` | `gpt-5` | The chat model that writes the note. |
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
| `source` | empty | Microphone override, a PulseAudio source name from `pactl list sources short`. Empty means the system default. |
| `sink` | empty | Speaker override; razorbill records this sink's monitor as the "Them" channel. |
| `echo_cancel` | `true` | Load PipeWire's echo-cancel module and route audio through it while the daemon runs, so speakers do not bleed into the mic. Defaults are restored on shutdown. |

## Notes

| option | default | notes |
|---|---|---|
| `notes_prompt_file` | empty | Path to a file that replaces the built-in note-writing prompt. Use this for your own template, a standup format, another language, and so on. The transcript and your jots are appended after the prompt. |

The built-in prompt asks for a title line, a summary, key points, decisions,
action items as checkboxes, and open questions, and tells the model to treat
your jots as anchors to expand.
