"""Configuration: ~/.config/razorbill/config.toml with sane defaults."""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

# Apps that legitimately capture the mic (or monitor streams) without being a
# meeting. Matched case-insensitively as substrings of application.name /
# process binary. `ignore_apps` from config is appended to this list.
BASE_IGNORE = [
    "razorbill",
    "pavucontrol",
    "pulseaudio volume control",
    "gnome-control-center",
    "kde-systemsettings",
    "plasmashell",
    "speech-dispatcher",
    "peak detect",
    "echo-cancel",
]

DEFAULT_PROMPT = """\
You turn a raw meeting transcript into concise, useful meeting notes.

The transcript has two speakers: "Me" is the note owner; "Them" is everyone
else on the call (they share one audio channel, so "Them" may be several
people; use names from the conversation when they are identifiable).

Write the notes as Markdown:
- First line: a level-1 heading with a short, specific title for the meeting
  (max 8 words, no date).
- Then these sections, omitting any that would be empty:
  ## Summary: 2-4 sentences.
  ## Key points: the substance of what was discussed.
  ## Decisions: only things actually decided.
  ## Action items: as "- [ ] task (owner)" checkboxes; infer owners when clear.
  ## Open questions

Use only information from the transcript and the owner's own notes. Do not
invent facts. If the owner jotted notes during the meeting, treat each jot as
an anchor: expand it with the relevant detail from the transcript and give it
priority in the output.
"""


@dataclass
class Config:
    # --- API (bring your own key) ---
    api_base: str = "https://api.openai.com/v1"
    api_key: str = ""            # or env OPENAI_API_KEY, or api_key_command
    api_key_command: str = ""    # e.g. "pass show openai" / "secret-tool lookup ..."
    transcribe_model: str = "gpt-4o-transcribe-diarize"
    notes_model: str = "gpt-5.5"
    language: str = ""           # optional ISO-639-1 hint for transcription
    # Optional per-service overrides, so transcription can use a different
    # (e.g. faster) OpenAI-compatible provider than note generation:
    transcribe_api_base: str = ""
    transcribe_api_key: str = ""
    notes_api_base: str = ""
    notes_api_key: str = ""

    # --- output ---
    output_dir: str = "~/Documents/meetings"
    keep_audio: bool = False     # delete audio after successful transcription
    notify: bool = True

    # --- detection / recording ---
    min_meeting_seconds: int = 60    # discard shorter recordings (mic tests etc.)
    grace_seconds: int = 20          # mic must be free this long before we stop
    poll_seconds: float = 2.0
    max_hours: float = 4.0           # hard stop safety cap
    segment_seconds: int = 600       # audio chunk size sent to the API
    ignore_apps: list[str] = field(default_factory=list)
    source: str = ""                 # override mic source (default: system default)
    sink: str = ""                   # override sink whose monitor is captured
    echo_cancel: bool = True         # speaker-safe recording, no headphones needed

    # --- live mode ---
    live_transcript: bool = False    # rolling transcript during the meeting (live.md)
    live_mode: str = "realtime"      # "realtime" (OpenAI websocket, ~1-2s lag),
                                     # "deepgram" (interim results mid-sentence),
                                     # or "segments" (per-segment batch calls)
    realtime_model: str = "gpt-realtime-whisper"
    deepgram_api_key: str = ""       # required for live_mode = "deepgram"
    deepgram_model: str = "nova-3"
    deepgram_diarize: bool = True    # split remote voices into Them (A)/(B) live
    live_insights: bool = False      # copilot pass on every live utterance
    insight_model: str = ""          # fast chat model for copilot passes; empty
                                     # falls back to notes_model (higher latency)
    insight_priority: bool = False   # OpenAI priority service tier for copilot
                                     # passes: lower latency, higher price
    context_dirs: list[str] = field(default_factory=list)  # background docs for notes/ask/insights

    # --- notes ---
    notes_prompt_file: str = ""      # replace the built-in prompt

    def all_ignores(self) -> list[str]:
        return BASE_IGNORE + [a.lower() for a in self.ignore_apps]

    def out_dir(self) -> Path:
        return Path(self.output_dir).expanduser()

    def pending_dir(self) -> Path:
        return self.out_dir() / ".pending"

    def resolve_key(self) -> str:
        key = os.environ.get("OPENAI_API_KEY", "") or self.api_key
        if not key and self.api_key_command:
            key = subprocess.run(
                self.api_key_command, shell=True, capture_output=True, text=True, timeout=30
            ).stdout.strip()
        return key

    def prompt(self) -> str:
        if self.notes_prompt_file:
            return Path(self.notes_prompt_file).expanduser().read_text()
        return DEFAULT_PROMPT


def config_path() -> Path:
    return Path(os.environ.get("RAZORBILL_CONFIG", "~/.config/razorbill/config.toml")).expanduser()


def save_api_key(key: str) -> Path:
    """Write the API key into the config file, creating it if needed."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = f'api_key = "{key}"'
    if path.exists():
        text = path.read_text()
        if re.search(r"^\s*api_key\s*=", text, flags=re.M):
            text = re.sub(r'^\s*api_key\s*=.*$', line, text, count=1, flags=re.M)
        else:
            text = f"{line}\n{text}"
        path.write_text(text)
    else:
        path.write_text(
            "# razorbill configuration. See config.example.toml for every option\n"
            f"{line}\n"
        )
    path.chmod(0o600)
    return path


def load() -> Config:
    cfg = Config()
    path = config_path()
    if path.exists():
        data = tomllib.loads(path.read_text())
        known = {f.name for f in fields(Config)}
        for k, v in data.items():
            if k not in known:
                raise SystemExit(f"config: unknown option {k!r} in {path}")
            setattr(cfg, k, v)
    return cfg
