"""Questions against the live meeting or the most recent note."""

from __future__ import annotations

from pathlib import Path

from . import context, meeting, openai_api, state
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


INSIGHT_SYSTEM = """\
You listen to a live meeting and decide whether anything is worth surfacing
to the note owner ("Me") right now, unprompted. Worth surfacing: a relevant
fact from the background documents about something just mentioned (a
customer, a product, a decision that contradicts the documents), a
commitment someone just made, or a concrete question the owner should ask
before the meeting ends. Not worth surfacing: summaries, restatements,
generic advice, anything already surfaced.

Reply with at most two short bullets, each one line. If nothing new is
clearly worth an interruption, reply with exactly: NONE
"""


def insight(cfg: Config, api: openai_api.Api, transcript_md: str, prior: str) -> str:
    """One proactive pass over the live transcript. Returns "" when silent."""
    parts = []
    docs = context.gather(cfg, api, transcript_md[-3000:])
    if docs:
        parts.append(f"Background documents:\n\n{docs}")
    parts.append(f"Transcript so far:\n\n{transcript_md[-8000:]}")
    parts.append(f"Already surfaced earlier (do not repeat):\n{prior[-2000:] or 'nothing yet'}")
    reply = openai_api.chat(cfg, api, INSIGHT_SYSTEM, "\n\n".join(parts)).strip()
    return "" if not reply or reply.upper().startswith("NONE") else reply
