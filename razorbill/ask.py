"""Questions against the live meeting or the most recent note."""

from __future__ import annotations

import re
from pathlib import Path

from . import context, events, meeting, openai_api, state
from .config import Config

LIVE_MD = "live.md"
COACH_DOC_CHARS = 8_000  # copilot doc budget; prefill time scales with input

# Utterances that suggest a call is wrapping up. Deliberately loose: this is
# only the trigger for the model confirmation below, so a false hit costs one
# tiny chat call, not a stopped recording.
FAREWELL_RE = re.compile(
    r"\b(good\s?bye|bye|see (you|ya)|talk (to you |too you )?(soon|later|next|tomorrow)"
    r"|take care|have a (good|great|nice)|thanks,? every(one|body)"
    r"|catch (you|ya)|cheers|signing off|drop( off)? now|got to (run|go|jump)"
    r"|have to (run|go|jump)|great (chat|call|meeting|talking)"
    r"|nice (chatting|talking|meeting))\b", re.IGNORECASE)

OVER_SYSTEM = """\
You judge whether a live meeting has just ended. You get the tail of its
transcript and how long ago the last words arrived. Answer YES only when the
conversation has clearly concluded: goodbyes exchanged, wrap-up finished,
participants leaving. A lull, a topic change, someone stepping away, or a
farewell that the conversation then moved past is NO. Answer exactly YES or NO.
"""


def meeting_over(cfg: Config, api: openai_api.Api, tail: str,
                 seconds_since_speech: float) -> bool:
    user = (f"Transcript tail:\n\n{tail[-2500:]}\n\n"
            f"The most recent words arrived {seconds_since_speech:.0f} seconds ago.")
    reply = openai_api.chat(cfg, api, OVER_SYSTEM, user, model=cfg.insight_model)
    return reply.strip().upper().startswith("YES")

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

A line marked "[now, being said]" is an utterance still in progress; treat
it as the freshest signal (for example, the start of a question you can
already prepare Me for), and expect it to be cut off mid-thought.

Hard rules: never summarize or restate the conversation, never repeat or
rephrase anything you already sent, no generic advice, no filler. At most
two bullets, each one line, telegraphic. When nothing new would help right
now, reply with exactly: SILENT
"""


def insight(cfg: Config, api: openai_api.Api, transcript_md: str, prior: str,
            docs: str | None = None, event: str = "") -> str:
    """One copilot pass over the live transcript. Returns "" when silent.

    `docs` lets the caller reuse a cached background-document selection;
    None gathers fresh (adds a selection call for large collections).
    `event` is the calendar block for this meeting, when known.
    """
    parts = []
    if event:
        parts.append(f"Calendar event for this meeting:\n{event}")
    if docs is None:
        docs = context.gather(cfg, api, transcript_md[-3000:], limit=COACH_DOC_CHARS)
    if docs:
        parts.append(f"Background documents:\n\n{docs}")
    parts.append(f"Transcript so far:\n\n{transcript_md[-4000:]}")
    parts.append(f"Already sent to Me (never repeat or rephrase these):\n"
                 f"{prior[-2000:] or 'nothing yet'}")
    reply = openai_api.chat(cfg, api, INSIGHT_SYSTEM, "\n\n".join(parts),
                            model=cfg.insight_model,
                            priority=cfg.insight_priority).strip()
    return "" if not reply or reply.upper().startswith(("SILENT", "NONE")) else reply
