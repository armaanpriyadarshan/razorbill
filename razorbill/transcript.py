"""Merge the me/them channel transcriptions into one speaker-labeled transcript."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

# Whisper hallucinates fillers ("Thanks for watching!") on silence; verbose_json
# flags those with a high no-speech probability.
NO_SPEECH_MAX = 0.6
COALESCE_GAP = 20.0  # join same-speaker utterances closer than this (seconds)

# Echo dedup: when recording without headphones, the mic also hears the
# speakers, so "Them" speech shows up a second time in the "Me" channel.
ECHO_WINDOW = 8.0        # seconds of timestamp slack between the two copies
ECHO_SIMILARITY = 0.75   # normalized text similarity to count as a duplicate


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", text.lower()).strip()


def dedupe_echo(me: list[dict], them: list[dict]) -> list[dict]:
    """Drop Me segments that are near-simultaneous near-duplicates of Them
    segments, which is speaker bleed picked up by the microphone."""
    out = []
    for m in me:
        nm = _norm(m.get("text", ""))
        is_echo = False
        if nm:
            for t in them:
                if abs(m["start"] - t["start"]) > ECHO_WINDOW:
                    continue
                nt = _norm(t.get("text", ""))
                if nt and difflib.SequenceMatcher(None, nm, nt).ratio() >= ECHO_SIMILARITY:
                    is_echo = True
                    break
        if not is_echo:
            out.append(m)
    return out


@dataclass
class Utterance:
    start: float
    speaker: str
    text: str


def merge(me: list[dict], them: list[dict]) -> list[Utterance]:
    # With a diarizing model, the Them channel distinguishes remote speakers;
    # label them "Them (A)", "Them (B)". The Me channel is always the owner.
    them_speakers = {s["speaker"] for s in them if s.get("speaker")}
    multi = len(them_speakers) > 1

    def label(seg: dict, who: str) -> str:
        if who == "Them" and multi and seg.get("speaker"):
            return f"Them ({seg['speaker']})"
        return who

    raw = [(s, "Me") for s in me] + [(s, "Them") for s in them]
    kept = [
        Utterance(s["start"], label(s, who), s["text"])
        for s, who in raw
        if s["text"] and s.get("no_speech_prob", 0.0) <= NO_SPEECH_MAX
    ]
    kept.sort(key=lambda u: u.start)

    merged: list[Utterance] = []
    last_end_guess = 0.0
    for u in kept:
        if merged and merged[-1].speaker == u.speaker and u.start - last_end_guess < COALESCE_GAP:
            merged[-1].text += " " + u.text
        else:
            merged.append(u)
        last_end_guess = u.start
    return merged


def _clock(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def to_markdown(utterances: list[Utterance]) -> str:
    return "\n\n".join(f"**[{_clock(u.start)}] {u.speaker}:** {u.text}" for u in utterances)
