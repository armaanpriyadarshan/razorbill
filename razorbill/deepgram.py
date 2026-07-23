"""Streaming transcription over Deepgram's realtime WebSocket.

Same job as `realtime.py` but against Deepgram's `/v1/listen`, with two
things the OpenAI path cannot do:

- Interim results: partial transcript text arrives within a few hundred
  milliseconds of the words being spoken, before the utterance ends.
- Live speaker attribution. With a system-audio device, audio is sent as
  stereo (mic left, system audio right) with `multichannel=true`, so every
  utterance is attributed to Me or Them by construction. Voice diarization
  on top splits multiple remote speakers into Them (A), Them (B).

Finalized utterances go to `on_line(text, label)`; growing partials go to
`on_partial(text)` pre-labeled, which powers the TUI's live caption of the
utterance still being spoken.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from collections.abc import Callable
from urllib.parse import urlencode

from . import audio
from .config import Config
from .ws import WebSocket, WsClosed

log = logging.getLogger("razorbill")

RATE = 24000
CHUNK_MS = 100
LETTERS = "ABCDEFGH"


class DeepgramStream:
    """Lifecycle wrapper: start() spawns the worker, shutdown() stops it."""

    def __init__(self, cfg: Config, source: str, monitor: str,
                 on_line: Callable[[str, str], None],
                 on_partial: Callable[[str], None] | None = None) -> None:
        self.cfg = cfg
        self.source = source
        self.monitor = monitor
        self.on_line = on_line
        self.on_partial = on_partial or (lambda _: None)
        self.channels = 2 if monitor else 1
        self.stop = threading.Event()
        self.proc: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self._finals: dict[int, list[tuple[str, int | None]]] = {}
        self._partials: dict[int, str] = {}
        self._letters: dict[int, dict[int, str]] = {}  # ch -> speaker id -> letter
        self._last_text = 0.0

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def pids(self) -> set[int]:
        p = self.proc
        return {p.pid} if p is not None and p.poll() is None else set()

    def shutdown(self) -> None:
        self.stop.set()
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
        if self.thread is not None:
            self.thread.join(timeout=10)

    # --- labeling ------------------------------------------------------------

    def _label(self, ch: int, speaker: int | None) -> str:
        """Mono streams stay unlabeled. Channel 0 is the mic; channel 1 is
        system audio, split by voice when diarization sees several people."""
        if self.channels == 1:
            return ""
        if ch == 0:
            return "Me"
        letters = self._letters.setdefault(ch, {})
        if speaker is not None and speaker not in letters:
            letters[speaker] = LETTERS[len(letters) % len(LETTERS)]
        if len(letters) <= 1 or speaker is None:
            return "Them"
        return f"Them ({letters[speaker]})"

    @staticmethod
    def _majority_speaker(chunks: list[tuple[str, int | None]]) -> int | None:
        counts: dict[int, int] = {}
        for _, s in chunks:
            if s is not None:
                counts[s] = counts.get(s, 0) + 1
        return max(counts, key=counts.get) if counts else None

    # --- event handling (pure, unit-testable) ------------------------------

    def handle(self, event: dict) -> None:
        if event.get("type") != "Results":
            return
        ch = (event.get("channel_index") or [0])[0]
        alt = ((event.get("channel") or {}).get("alternatives") or [{}])[0]
        text = (alt.get("transcript") or "").strip()
        words = alt.get("words") or []
        speaker = self._majority_speaker([("", w.get("speaker")) for w in words])
        finals = self._finals.setdefault(ch, [])

        if event.get("is_final"):
            if text:
                finals.append((text, speaker))
                self._last_text = time.monotonic()
            if event.get("speech_final"):
                self._flush(ch)
            elif text:
                self._emit_partial(ch, None)
        elif text:
            self._last_text = time.monotonic()
            self._emit_partial(ch, text)

    def _emit_partial(self, ch: int, interim: str | None) -> None:
        chunks = self._finals.get(ch, [])
        parts = [t for t, _ in chunks]
        if interim:
            parts.append(interim)
        text = " ".join(parts).strip()
        label = self._label(ch, self._majority_speaker(chunks))
        self._partials[ch] = (f"{label}: {text}" if label else text) if text else ""
        self._push_partials()

    def _push_partials(self) -> None:
        live = [v for _, v in sorted(self._partials.items()) if v]
        self.on_partial("\n".join(live))

    def _flush(self, ch: int) -> None:
        chunks = self._finals.pop(ch, [])
        text = " ".join(t for t, _ in chunks).strip()
        if text:
            self.on_line(text, self._label(ch, self._majority_speaker(chunks)))
        self._partials.pop(ch, None)
        self._push_partials()

    def _flush_all(self) -> None:
        for ch in list(self._finals):
            self._flush(ch)

    # --- session loop -------------------------------------------------------

    def _url(self) -> str:
        params = {
            "model": self.cfg.deepgram_model,
            "encoding": "linear16",
            "sample_rate": RATE,
            "channels": self.channels,
            "interim_results": "true",
            "smart_format": "true",
            "endpointing": 300,
        }
        if self.channels > 1:
            params["multichannel"] = "true"
        if self.cfg.deepgram_diarize:
            params["diarize"] = "true"
        if self.cfg.language:
            params["language"] = self.cfg.language
        return f"wss://api.deepgram.com/v1/listen?{urlencode(params)}"

    def _run(self) -> None:
        backoff = 2.0
        while not self.stop.is_set():
            try:
                self._session()
                backoff = 2.0
            except Exception as e:
                if self.stop.is_set():
                    return
                log.warning("deepgram stream error, reconnecting: %s", e)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _session(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            self.proc = (audio.stereo_pcm(self.source, self.monitor, RATE)
                         if self.channels == 2
                         else audio.mixed_pcm(self.source, "", RATE))
        ws = WebSocket(self._url(),
                       {"Authorization": f"Token {self.cfg.deepgram_api_key}"},
                       timeout=15)
        ws.set_timeout(1.0)
        try:
            sender = threading.Thread(target=self._pump_audio, args=(ws,), daemon=True)
            sender.start()
            while not self.stop.is_set():
                try:
                    event = json.loads(ws.recv_text())
                except TimeoutError:
                    # endpointing missed (e.g. steady background noise):
                    # do not let settled utterances sit around
                    if self._finals and time.monotonic() - self._last_text > 3.0:
                        self._flush_all()
                    continue
                self.handle(event)
        finally:
            self._flush_all()
            ws.close()

    def _pump_audio(self, ws: WebSocket) -> None:
        chunk_bytes = RATE * 2 * self.channels * CHUNK_MS // 1000
        try:
            while not self.stop.is_set():
                chunk = self.proc.stdout.read(chunk_bytes)
                if not chunk:
                    ws.send_text(json.dumps({"type": "CloseStream"}))
                    return
                ws.send_binary(chunk)
        except (OSError, WsClosed):
            pass  # the receive loop handles reconnects
