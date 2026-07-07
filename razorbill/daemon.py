"""The always-on watcher: detect meetings, record, then process in the background."""

from __future__ import annotations

import datetime as dt
import logging
import signal
import threading
import time
from pathlib import Path

from . import ask, audio, meeting, openai_api, realtime, state
from .config import Config
from .notify import notify, notify_action

log = logging.getLogger("razorbill")

MANUAL = "manual"


class Daemon:
    def __init__(self, cfg: Config, api: openai_api.Api) -> None:
        self.cfg = cfg
        self.api = api
        self.rec = audio.Recorder()
        self.ec = audio.EchoCancel()
        self.dir: Path | None = None
        self.app = ""
        self.started = 0.0
        self.last_mic_activity = 0.0
        self.processing = 0
        self.stop_flag = False
        self._live: threading.Thread | None = None
        self._rt: realtime.Realtime | None = None
        self._insight_running = False
        self._last_insight = 0.0

    # --- state file ----------------------------------------------------------
    def _write_status(self) -> None:
        if self.dir:
            status = {"state": "recording", "dir": str(self.dir), "app": self.app, "since": self.started}
        elif self.processing > 0:
            status = {"state": "processing"}
        else:
            status = {"state": "idle"}
        state.write_status(status)

    # --- meeting lifecycle -----------------------------------------------------
    def _start(self, app: str) -> None:
        if self.ec.active:
            # Record "them" from the real sink's monitor; it carries everything
            # (including apps pinned past the echo-cancel sink).
            source, monitor = audio.EC_SOURCE, f"{self.ec.prev_sink}.monitor"
        else:
            source, monitor = audio.default_source(self.cfg), audio.default_monitor(self.cfg)
        if not source:
            log.error("no microphone configured; set `source` in the config "
                      "(see docs/configuration.md, platform devices)")
            notify(self.cfg.notify, "razorbill: cannot record",
                   "No microphone configured. Set `source` in the config.")
            return
        self.dir = meeting.new_meeting_dir(self.cfg, app)
        self.app = app
        self.started = self.last_mic_activity = time.time()
        self.rec.start(self.dir, self.cfg, source, monitor)
        if self.cfg.live_transcript and self.cfg.live_mode == "realtime":
            self._rt = realtime.Realtime(
                self.cfg, self.api, source, monitor,
                on_line=self._make_line_sink(self.dir),
            )
            self._rt.start()
        log.info("recording started (%s) -> %s", app, self.dir)
        threading.Thread(target=self._offer_stop, args=(app,), daemon=True).start()

    def _make_line_sink(self, d: Path):
        lock = threading.Lock()

        def on_line(text: str) -> None:
            stamp = time.strftime("%H:%M:%S")
            with lock, (d / meeting.LIVE_MD).open("a") as f:
                f.write(f"**[{stamp}]** {text}\n\n")
            if self.cfg.live_insights:
                self._maybe_insight(d)

        return on_line

    def _maybe_insight(self, d: Path) -> None:
        now = time.time()
        if self._insight_running or now - self._last_insight < self.cfg.insight_interval:
            return
        self._insight_running = True
        self._last_insight = now

        def work() -> None:
            try:
                md = (d / meeting.LIVE_MD).read_text()
                self._insight_pass(d, md)
            except Exception as e:
                log.warning("insight pass failed: %s", e)
            finally:
                self._insight_running = False

        threading.Thread(target=work, daemon=True).start()

    def _offer_stop(self, app: str) -> None:
        if notify_action(self.cfg.notify, "Recording meeting",
                         f"Detected: {app}", "stop", "Stop"):
            state.request_stop()

    def _finish(self) -> None:
        d, app = self.dir, self.app
        assert d is not None
        self.rec.stop()
        self.dir = None
        if self._rt is not None:
            self._rt.shutdown()
            self._rt = None
        if self._live is not None and self._live.is_alive():
            self._live.join(timeout=60)  # let the last live pass save its cache
        elapsed = time.time() - self.started

        # Auto-detected blips (mic checks) get discarded; manual recordings are
        # always kept: the user explicitly asked for them.
        if app != MANUAL and elapsed < self.cfg.min_meeting_seconds:
            log.info("discarding %.0fs auto recording (< min_meeting_seconds)", elapsed)
            notify(self.cfg.notify, "Recording discarded",
                   f"{app}: only {elapsed:.0f}s of audio, treated as a mic check. No notes.")
            meeting.discard(d)
            return

        meta = meeting.read_meta(d)
        meta |= {"ended": dt.datetime.now().isoformat(timespec="seconds"), "status": "recorded"}
        meeting.write_meta(d, meta)
        log.info("meeting ended (%s, %.0f min); processing", app, elapsed / 60)
        self.processing += 1
        threading.Thread(target=self._process, args=(d,), daemon=False).start()

    def _process(self, d: Path) -> None:
        try:
            out = meeting.process(self.cfg, self.api, d)
            log.info("notes written: %s", out)
            threading.Thread(target=self._offer_open, args=(out,), daemon=True).start()
        except Exception as e:  # keep the daemon alive whatever the API does
            log.error("processing failed, audio kept for `razorbill reprocess`: %s", e)
            notify(self.cfg.notify, "razorbill: processing failed",
                   f"{e}\nAudio kept. Run: razorbill reprocess")
        finally:
            self.processing -= 1

    def _offer_open(self, path: Path) -> None:
        if notify_action(self.cfg.notify, "Meeting notes ready", str(path), "open", "Open"):
            state.open_path(path)

    # --- main loop ---------------------------------------------------------------
    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        self.cfg.out_dir().mkdir(parents=True, exist_ok=True)

        if self.cfg.echo_cancel and audio.PLATFORM == "linux":
            if self.ec.enable(self.cfg):
                log.info("echo cancellation active; no headphones needed")
            else:
                log.warning("echo-cancel module unavailable; recording without it "
                            "(wear headphones or set echo_cancel = false)")
        if audio.detection_supported():
            log.info("watching for meetings (mic capture by other apps)")
        else:
            log.info("automatic detection is not available on %s; start recordings "
                     "manually (TUI, `razorbill start`, or `razorbill toggle`)",
                     audio.PLATFORM)

        while not self.stop_flag:
            try:
                self._tick()
            except Exception as e:
                log.error("tick failed: %s", e)
            self._write_status()
            time.sleep(self.cfg.poll_seconds)

        if self.dir:
            self._finish()
        self.ec.disable()
        state.clear_status()
        log.info("stopped")

    def _tick(self) -> None:
        manual_start = state.consume_start_request()
        ours = self.rec.pids() | (self._rt.pids() if self._rt else set())
        apps = audio.mic_capture_apps(self.cfg, exclude_pids=ours)
        now = time.time()

        if self.dir is None:
            if manual_start:
                self._start(MANUAL)
            elif apps:
                self._start(apps[0])
            return

        # recording: check the stop conditions
        if apps:
            self.last_mic_activity = now
        stop_requested = (self.dir / "stop").exists()
        auto = self.app != MANUAL
        idle_too_long = auto and (now - self.last_mic_activity) > self.cfg.grace_seconds
        over_cap = (now - self.started) > self.cfg.max_hours * 3600
        died = not self.rec.alive()

        if stop_requested or idle_too_long or over_cap or died:
            if died:
                log.error("recorder exited unexpectedly, see ffmpeg.log in %s", self.dir)
            self._finish()
            return

        if (self.cfg.live_transcript and self.cfg.live_mode == "segments"
                and (self._live is None or not self._live.is_alive())):
            self._live = threading.Thread(target=self._live_pass, args=(self.dir,), daemon=True)
            self._live.start()

    def _live_pass(self, d: Path) -> None:
        """Transcribe finished segments during the meeting and, when enabled,
        run a proactive insight pass over the growing transcript."""
        try:
            cache = meeting.load_seg_cache(d)
            pending = [p for p in meeting.completed_segments(d) if p.name not in cache]
            if not pending:
                return
            for p in pending[:2]:  # bounded work per pass; the tick respawns us
                cache[p.name] = openai_api.transcribe(
                    self.cfg, self.api, p, offset=meeting.segment_offset(self.cfg, p))
            meeting.save_seg_cache(d, cache)
            md = meeting.live_markdown(cache)
            (d / meeting.LIVE_MD).write_text(md)
            if self.cfg.live_insights and md:
                self._insight_pass(d, md)
        except Exception as e:  # the final processing pass will catch up
            log.warning("live pass failed: %s", e)

    def _insight_pass(self, d: Path, transcript_md: str) -> None:
        insights = d / meeting.INSIGHTS_MD
        prior = insights.read_text() if insights.exists() else ""
        reply = ask.insight(self.cfg, self.api, transcript_md, prior)
        if not reply:
            return
        stamp = dt.datetime.now().strftime("%H:%M")
        with insights.open("a") as f:
            f.write(f"[{stamp}] {reply}\n\n")
        log.info("insight: %s", reply.replace("\n", " / "))
        notify(self.cfg.notify, "razorbill", reply[:220])

    def _on_signal(self, signum, frame) -> None:  # noqa: ARG002
        self.stop_flag = True


def run(cfg: Config) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        api = openai_api.resolve(cfg)
    except openai_api.ApiError as e:
        raise SystemExit(str(e)) from None
    Daemon(cfg, api).run()
