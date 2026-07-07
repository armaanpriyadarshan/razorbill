"""Streaming transcription over the OpenAI realtime WebSocket.

One ffmpeg process mixes the microphone and system audio into a single
24 kHz mono PCM stream; a sender thread feeds it to the realtime endpoint
and the session loop collects finished utterances. Each utterance is passed
to a callback within a couple of seconds of the words being spoken.

This stream powers the live transcript and insights only. The segmented
recording continues in parallel and the final note is still built from the
batch pipeline, which keeps channel separation and diarization.
"""

from __future__ import annotations

import base64
import json
import logging
import subprocess
import threading
import time
from collections.abc import Callable

from . import audio
from .config import Config
from .openai_api import Api
from .ws import WebSocket, WsClosed

log = logging.getLogger("razorbill")

CHUNK_BYTES = 9600  # 200 ms of 24 kHz mono pcm16


def _ws_url(base: str) -> str:
    root = base.rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    return root.replace("https://", "wss://", 1) + "/v1/realtime?intent=transcription"


class Realtime:
    """Lifecycle wrapper: start() spawns the worker, shutdown() stops it."""

    def __init__(self, cfg: Config, api: Api, source: str, monitor: str,
                 on_line: Callable[[str], None]) -> None:
        self.cfg = cfg
        self.api = api
        self.source = source
        self.monitor = monitor
        self.on_line = on_line
        self.stop = threading.Event()
        self.proc: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None

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

    # --- internals -------------------------------------------------------

    def _spawn_ffmpeg(self) -> subprocess.Popen:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
               *audio._input_args(self.source)]
        if self.monitor:
            # normalize=0: keep both inputs at full amplitude (the default
            # halves each, which starves the far end's VAD of signal)
            cmd += [*audio._input_args(self.monitor),
                    "-filter_complex", "amix=inputs=2:duration=longest:normalize=0"]
        cmd += ["-ac", "1", "-ar", "24000", "-f", "s16le", "pipe:1"]
        return subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def _run(self) -> None:
        backoff = 2.0
        while not self.stop.is_set():
            try:
                self._session()
                backoff = 2.0
            except Exception as e:
                if self.stop.is_set():
                    return
                log.warning("realtime stream error, reconnecting: %s", e)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _session(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            self.proc = self._spawn_ffmpeg()
        ws = WebSocket(_ws_url(self.api.transcribe_base),
                       {"Authorization": f"Bearer {self.api.transcribe_key}"},
                       timeout=15)
        ws.set_timeout(1.0)  # post-handshake: recv doubles as the flush tick
        # Utterance assembly. The GA endpoint streams word deltas per item;
        # a "completed" event is not guaranteed (gpt-realtime-whisper emits
        # none), so deltas are the source of truth. A buffered utterance is
        # flushed when a new item starts, on inactivity, or at shutdown.
        # Deltas for one utterance arrive in a burst, so a short idle window
        # is safe; it bounds how stale a finished utterance can sit buffered.
        item_id: str | None = None
        buffer = ""
        last_delta = time.monotonic()

        def flush() -> None:
            nonlocal buffer, item_id
            text = buffer.strip()
            buffer, item_id = "", None
            if text:
                self.on_line(text)

        try:
            json.loads(ws.recv_text())  # session.created
            ws.send_text(json.dumps({
                "type": "session.update",
                "session": {
                    "type": "transcription",
                    "audio": {"input": {"transcription": {"model": self.cfg.realtime_model}}},
                },
            }))
            sender = threading.Thread(target=self._pump_audio, args=(ws,), daemon=True)
            sender.start()

            while not self.stop.is_set():
                try:
                    event = json.loads(ws.recv_text())
                except TimeoutError:
                    if buffer and time.monotonic() - last_delta > 1.2:
                        flush()
                    continue
                kind = event.get("type", "")
                if kind == "conversation.item.input_audio_transcription.delta":
                    if item_id is not None and event.get("item_id") != item_id:
                        flush()
                    item_id = event.get("item_id")
                    buffer += event.get("delta", "")
                    last_delta = time.monotonic()
                elif kind == "conversation.item.input_audio_transcription.completed":
                    text = (event.get("transcript") or "").strip()
                    buffer, item_id = "", None
                    if text:
                        self.on_line(text)
                elif kind == "error":
                    raise WsClosed(json.dumps(event.get("error", {}))[:300])
        finally:
            flush()
            ws.close()

    def _pump_audio(self, ws: WebSocket) -> None:
        try:
            while not self.stop.is_set():
                chunk = self.proc.stdout.read(CHUNK_BYTES)
                if not chunk:
                    return  # recorder ended; the meeting is over
                ws.send_text(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(chunk).decode(),
                }))
        except (OSError, WsClosed):
            pass  # the receive loop handles reconnects
