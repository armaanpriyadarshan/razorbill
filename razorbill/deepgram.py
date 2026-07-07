"""Streaming transcription over Deepgram's realtime WebSocket.

Same job as `realtime.py` but against Deepgram's `/v1/listen`, whose
distinguishing feature is interim results: partial transcript text arrives
within a few hundred milliseconds of the words being spoken, before the
utterance ends. Finals (on endpointing) go to `on_line` like the OpenAI
path; growing partials go to `on_partial`, which lets the live copilot
react to a question while it is still being asked.

Audio is sent as raw binary pcm16 frames; results come back as JSON
"Results" events carrying `is_final` (this chunk's text is settled) and
`speech_final` (the utterance is over).
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

CHUNK_BYTES = 4800  # 100 ms of 24 kHz mono pcm16
RATE = 24000


def _url(cfg: Config) -> str:
    params = {
        "model": cfg.deepgram_model,
        "encoding": "linear16",
        "sample_rate": RATE,
        "channels": 1,
        "interim_results": "true",
        "smart_format": "true",
        "endpointing": 300,
    }
    if cfg.language:
        params["language"] = cfg.language
    return f"wss://api.deepgram.com/v1/listen?{urlencode(params)}"


class DeepgramStream:
    """Lifecycle wrapper: start() spawns the worker, shutdown() stops it."""

    def __init__(self, cfg: Config, source: str, monitor: str,
                 on_line: Callable[[str], None],
                 on_partial: Callable[[str], None] | None = None) -> None:
        self.cfg = cfg
        self.source = source
        self.monitor = monitor
        self.on_line = on_line
        self.on_partial = on_partial or (lambda _: None)
        self.stop = threading.Event()
        self.proc: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self._finals: list[str] = []  # settled chunks of the open utterance
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

    # --- event handling (pure, unit-testable) ------------------------------

    def handle(self, event: dict) -> None:
        if event.get("type") != "Results":
            return
        alts = (event.get("channel") or {}).get("alternatives") or [{}]
        text = (alts[0].get("transcript") or "").strip()
        if event.get("is_final"):
            if text:
                self._finals.append(text)
                self._last_text = time.monotonic()
            if event.get("speech_final"):
                self._flush()
            elif text:
                self.on_partial(" ".join(self._finals))
        elif text:
            self._last_text = time.monotonic()
            self.on_partial(" ".join([*self._finals, text]))

    def _flush(self) -> None:
        utterance = " ".join(self._finals).strip()
        self._finals = []
        if utterance:
            self.on_line(utterance)
        self.on_partial("")

    # --- session loop -------------------------------------------------------

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
            self.proc = audio.mixed_pcm(self.source, self.monitor, RATE)
        ws = WebSocket(_url(self.cfg),
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
                    # do not let a settled utterance sit around
                    if self._finals and time.monotonic() - self._last_text > 3.0:
                        self._flush()
                    continue
                self.handle(event)
        finally:
            self._flush()
            ws.close()

    def _pump_audio(self, ws: WebSocket) -> None:
        try:
            while not self.stop.is_set():
                chunk = self.proc.stdout.read(CHUNK_BYTES)
                if not chunk:
                    ws.send_text(json.dumps({"type": "CloseStream"}))
                    return
                ws.send_binary(chunk)
        except (OSError, WsClosed):
            pass  # the receive loop handles reconnects
