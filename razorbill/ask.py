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
You are a silent copilot for one side of a live call ("Me"; the other
participants are "Them"). You run after every new utterance, seeing the
transcript so far, background documents, and everything you already sent.
Decide whether a short message would help Me in this moment. Send one when:

- Them just asked Me a question: suggest the answer in one line, grounded
  in the background documents when they apply.
- A company, product, tool, metric, or strategy came up that the documents
  know about, or that deserves a one-line explanation.
- Them is running an identifiable play (an intro framing, a pricing anchor,
  an objection script): name it and what it means for Me.
- A topic is wrapping up: the one follow-up question Me should ask.

Hard rules: never summarize or restate the conversation, never repeat or
rephrase anything you already sent, no generic advice, no filler. At most
two bullets, each one line, telegraphic. When nothing new would help right
now, reply with exactly: SILENT
"""


def insight(cfg: Config, api: openai_api.Api, transcript_md: str, prior: str,
            docs: str | None = None) -> str:
    """One copilot pass over the live transcript. Returns "" when silent.

    `docs` lets the caller reuse a cached background-document selection;
    None gathers fresh (adds a selection call for large collections).
    """
    parts = []
    if docs is None:
        docs = context.gather(cfg, api, transcript_md[-3000:])
    if docs:
        parts.append(f"Background documents:\n\n{docs}")
    parts.append(f"Transcript so far:\n\n{transcript_md[-8000:]}")
    parts.append(f"Already sent to Me (never repeat or rephrase these):\n"
                 f"{prior[-2000:] or 'nothing yet'}")
    reply = openai_api.chat(cfg, api, INSIGHT_SYSTEM, "\n\n".join(parts),
                            model=cfg.insight_model).strip()
    return "" if not reply or reply.upper().startswith(("SILENT", "NONE")) else reply
