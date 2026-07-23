"""Questions against the live meeting or the most recent note."""

from __future__ import annotations

from pathlib import Path

from . import context, events, meeting, openai_api, state
from .config import Config

LIVE_MD = "live.md"

SYSTEM = """\
You answer questions about a meeting. You get background documents (when
available), a transcript or meeting note, and a question. Answer directly
and concretely from that material; say plainly when it does not contain the
answer. "Me" is the person asking; "Them" is the other participants.
"""


def answer(cfg: Config, api: openai_api.Api, question: str) -> str:
    parts: list[str] = []

    s = state.read_status()
    if s.get("state") == "recording":
        block = events.describe(events.read_event(Path(s["dir"])))
        if block:
            parts.append(f"Calendar event for this meeting:\n{block}")
        live = Path(s["dir"]) / LIVE_MD
        transcript = live.read_text().strip() if live.exists() else ""
        if transcript:
            parts.append(f"Live transcript of the meeting in progress "
                         f"(lags a few minutes behind):\n\n{transcript}")
        else:
            parts.append("A meeting is being recorded but no transcript is "
                         "available yet (live_transcript may be off, or the "
                         "first segment has not completed).")
    else:
        notes = meeting.list_notes(cfg)
        if notes:
            body = notes[0]["path"].read_text()
            parts.append(f"Most recent meeting note ({notes[0]['title']}):\n\n{body}")
        else:
            parts.append("There are no meeting notes yet.")

    docs = context.gather(cfg, api, question)
    if docs:
        parts.insert(0, f"Background documents:\n\n{docs}")

    parts.append(f"Question: {question}")
    return openai_api.chat(cfg, api, SYSTEM, "\n\n".join(parts))
